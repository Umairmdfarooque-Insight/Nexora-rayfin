import argparse
import contextlib
import io
import json
import sys
import threading
import time
from typing import Callable

import requests

CHECK_TIMEOUT_SECONDS = 30
SCHEMA_NAME = "saleslt"
DETAIL_MAX_LENGTH = 200
CONTROL_SMOKE_PATH = "runs/_smoke/check.txt"


class CheckFailed(Exception):
    """A check failed for a reason that is already readable."""


class CheckSkipped(Exception):
    """A check doesn't apply in this configuration."""


def _require_settings(*names: str) -> None:
    import app.config as config

    missing = [name for name in names if not getattr(config, name)]

    if missing:
        raise CheckFailed(f"Missing configuration: {', '.join(missing)}")


def check_llm() -> str:
    from app.config import AZURE_OPENAI_DEPLOYMENT
    from app.llm.client import client

    response = client.responses.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        input="Reply with the word OK.",
        max_output_tokens=256,
    )

    if not response.output_text:
        raise CheckFailed("The AI deployment returned an empty response")

    return "Azure OpenAI replied"


def check_lakehouse() -> str:
    _require_settings(
        "FABRIC_WORKSPACE_ID",
        "FABRIC_LAKEHOUSE_ID",
        "FABRIC_LAKEHOUSE_NAME",
    )

    from app.connectors.fabric_lakehouse import FabricLakehouseConnector

    tables = FabricLakehouseConnector().list_tables(SCHEMA_NAME)

    return f"{len(tables)} tables in {SCHEMA_NAME}"


def check_sql() -> str:
    _require_settings("FABRIC_SQL_SERVER", "FABRIC_SQL_DATABASE")

    from app.connectors.fabric_sql_connector import FabricSQLConnector

    with FabricSQLConnector().connect() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")

        if cursor.fetchone()[0] != 1:
            raise CheckFailed("SELECT 1 returned an unexpected result")

    return "SELECT 1 succeeded"


def check_control() -> str:
    import app.config as config

    if config.RUN_STORE == "file":
        raise CheckSkipped("RUN_STORE=file; no control store to check")

    _require_settings("FABRIC_CONTROL_DB_SERVER", "FABRIC_CONTROL_DB_NAME")

    home_workspace = config.NEXORA_HOME_WORKSPACE_ID
    control_lakehouse = config.NEXORA_CONTROL_LAKEHOUSE_ID
    if not home_workspace or not control_lakehouse:
        raise CheckFailed(
            "Missing configuration: NEXORA_HOME_WORKSPACE_ID, NEXORA_CONTROL_LAKEHOUSE_ID"
        )

    from app.connectors.fabric_sql_connector import FabricSQLConnector

    with FabricSQLConnector(config.FABRIC_CONTROL_DB_SERVER, config.FABRIC_CONTROL_DB_NAME).connect() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")

        if cursor.fetchone()[0] != 1:
            raise CheckFailed("SELECT 1 on the control database returned an unexpected result")

    from app.connectors.onelake_files import delete_file, write_file

    try:
        write_file(home_workspace, control_lakehouse, CONTROL_SMOKE_PATH, b"smoke")
        delete_file(home_workspace, control_lakehouse, CONTROL_SMOKE_PATH)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status in (401, 403):
            raise CheckFailed("FORBIDDEN: cannot write to the control Lakehouse") from None
        raise CheckFailed(f"Control Lakehouse write failed (HTTP {status})") from None

    return "control database and control Lakehouse are reachable"


def check_settings() -> str:
    import app.config as config

    names = config.required_setting_names()
    missing = [name for name in names if config.get_optional_setting(name) is None]

    if missing:
        raise CheckFailed(f"Missing configuration: {', '.join(missing)}")

    parts = [f"{name} from {config.setting_source(name)}" for name in names]
    return "; ".join(parts) if parts else "no required settings for this runtime"


CHECKS: dict[str, Callable[[], str]] = {
    "llm": check_llm,
    "lakehouse": check_lakehouse,
    "sql": check_sql,
    "control": check_control,
    "settings": check_settings,
}


def _describe(error: BaseException, secrets: list[str]) -> str:
    if isinstance(error, (CheckFailed, CheckSkipped)):
        text = str(error)
    else:
        text = f"{type(error).__name__}: {error}"

    text = " ".join(text.split())

    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")

    return text[:DETAIL_MAX_LENGTH]


def run_check(name: str, secrets: list[str]) -> dict:
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["detail"] = CHECKS[name]()
        except CheckSkipped as skip:
            outcome["skip"] = skip
        except BaseException as error:  # a failing check must not stop the others
            outcome["error"] = error

    started = time.monotonic()
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(CHECK_TIMEOUT_SECONDS)
    duration_ms = int((time.monotonic() - started) * 1000)

    if worker.is_alive():
        status, detail = "fail", f"timed out after {CHECK_TIMEOUT_SECONDS}s"
    elif "error" in outcome:
        status, detail = "fail", _describe(outcome["error"], secrets)
    elif "skip" in outcome:
        status, detail = "skip", _describe(outcome["skip"], secrets)
    else:
        status, detail = "pass", outcome["detail"]

    return {
        "name": name,
        "status": status,
        "duration_ms": duration_ms,
        "detail": detail,
    }


def format_text(results: list[dict]) -> str:
    lines = []

    for result in results:
        seconds = result["duration_ms"] / 1000
        lines.append(
            f"{result['status'].upper():<6}{result['name']:<11}"
            f"{result['detail']} ({seconds:.1f}s)"
        )

    return "\n".join(lines)


def parse_only(value: str | None) -> list[str]:
    if value is None:
        return list(CHECKS)

    names = [part.strip() for part in value.split(",") if part.strip()]
    invalid = [name for name in names if name not in CHECKS]

    if invalid or not names:
        raise ValueError(
            f"Invalid --only value: {value!r}. "
            f"Valid checks: {', '.join(CHECKS)}"
        )

    return list(dict.fromkeys(names))


def run_checks(names: list[str], secrets: list[str]) -> dict:
    """Run `names` and return `{"passed", "checks"}`. A "fail" status fails the run;
    "pass" and "skip" don't. Shared by the CLI and the Fabric job entry point."""
    # The connectors print raw API responses while they run; keep them out of the output.
    with contextlib.redirect_stdout(io.StringIO()):
        results = [run_check(name, secrets) for name in names]

    passed = all(result["status"] != "fail" for result in results)
    return {"passed": passed, "checks": results}


def run_job(only: str | None = None) -> dict:
    """Entry point for `nexora_stage_runner` with `operation = "smoke"` (T-102 wires
    this in). Prints the text report to the notebook output and returns the same JSON
    shape the CLI prints with `--json`, for `notebookutils.notebook.exit(...)`."""
    names = parse_only(only)

    import app.config as config

    secrets = [config.AZURE_OPENAI_API_KEY]
    result = run_checks(names, secrets)

    print(format_text(result["checks"]))

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.smoke",
        description="Check the LLM endpoint, the Lakehouse, the SQL endpoint, the control store and settings.",
    )
    parser.add_argument(
        "--only",
        help=f"comma-separated checks to run ({', '.join(CHECKS)})",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    try:
        names = parse_only(args.only)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    try:
        import app.config as config
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    from app.runtime import RUNTIME

    if RUNTIME == "local":
        missing = [
            name for name in config.required_setting_names()
            if config.get_optional_setting(name) is None
        ]
        if missing:
            print(f"Missing configuration: {', '.join(missing)}", file=sys.stderr)
            return 2

    secrets = [config.AZURE_OPENAI_API_KEY]

    result = run_checks(names, secrets)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_text(result["checks"]))

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
