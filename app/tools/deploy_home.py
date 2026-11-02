"""python -m app.tools.deploy_home --env dev|test|prod [--create NAME] [--dry-run]

Creates or updates the Nexora home workspace's items (CLAUDE.md "Target hosting",
T-005) from the definitions committed under `fabric/`, in either the workspace in
NEXORA_HOME_WORKSPACE_ID or, the first time, the one named with --create (reusing
an existing workspace of that name if one already exists). Any workspace name is
accepted -- the home workspace can be a dedicated one or an existing data
workspace, at the caller's own choice.

The UserDataFunction and VariableLibrary payload shapes were confirmed, and fixed,
against a live tenant on 2026-11-01: both need their own dedicated collection
endpoint rather than the generic `POST /items` (UserDataFunction), and both were
missing required definition parts (VariableLibrary's `settings.json`; UserDataFunction's
`definition.json` and `resources/functions.json`, and its Python file had the wrong
name -- Fabric requires `function_app.py`, not `function-app.py`). See
fabric-hosting.md's platform-limits table for anything still open after that fix
(e.g. whether a UDF can attach nexora_env, confirmed separately).
"""

import argparse
import base64
import json
import sys
from pathlib import Path

from app.config import get_optional_setting
from app.connectors.fabric_api import FabricApiClient
from app.connectors.onelake_files import write_file
from app.deploy.fabric_items import (
    FabricItemsClient,
    FabricOperationFailed,
    FabricTimeout,
    FabricUpstreamError,
    NameConflict,
)
from app.tools.package import PackageError, package

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FABRIC_DIR = PROJECT_ROOT / "fabric"
CONFIG_DIR = PROJECT_ROOT / "config"
BUILD_MANIFEST = PROJECT_ROOT / "build" / "manifest.json"

ENVIRONMENT_ITEM = "nexora_env"
API_ITEM = "nexora_api"
STAGE_RUNNER_ITEM = "nexora_stage_runner"
CONTROL_DB_ITEM = "nexora_control_db"
CONTROL_LAKEHOUSE_ITEM = "nexora_control"
SETTINGS_ITEM = "nexora_settings"
TELEMETRY_ITEM = "nexora_telemetry"


class BadArguments(Exception):
    """Exit 2: a bad argument or an invalid target. Nothing was deployed."""


class DeployError(Exception):
    """Exit 1: a Fabric operation failed."""


def _folder_parts(folder: Path) -> list[dict]:
    """Definition parts of a Fabric Git-format item folder: every file under it,
    `.platform` included, as Fabric stores it in Git."""
    parts = []
    for file in sorted(p for p in folder.rglob("*") if p.is_file()):
        parts.append(
            {
                "path": file.relative_to(folder).as_posix(),
                "payload": base64.b64encode(file.read_bytes()).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        )
    return parts


def notebook_definition(workspace_id: str, environment_id: str | None) -> dict:
    """`notebook-content.ipynb` and `.platform`. `environment_id` (nexora_env's own
    id, once it exists) is written into the notebook's `metadata.dependencies.environment`
    so the job has the `nexora` wheel installed -- without it the notebook runs on
    Fabric's bare base runtime, which has no `app` package at all (confirmed against
    a live tenant, 2026-11-02: every job failed with `System_Cancelled_Session_
    Statements_Failed` before ever reaching `app.fabric.job.run_job`). Whether
    `updateDefinition` with `format: ipynb` actually applies this metadata is itself
    unconfirmed (a community report describes it being honored only for the
    `fabricGitSource` .py format) -- if the next deploy's manual test still fails the
    same way, attach `nexora_env` to this item by hand in the portal instead (Notebook
    ribbon -> Environment), the same kind of one-time step the UDF's Manage Connections
    binding already needs."""
    parts = _folder_parts(FABRIC_DIR / f"{STAGE_RUNNER_ITEM}.Notebook")
    if environment_id:
        for part in parts:
            if part["path"] == "notebook-content.ipynb":
                notebook = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
                notebook.setdefault("metadata", {})["dependencies"] = {
                    "environment": {"environmentId": environment_id, "workspaceId": workspace_id}
                }
                part["payload"] = base64.b64encode(
                    (json.dumps(notebook, indent=1) + "\n").encode("utf-8")
                ).decode("ascii")
                break
    return {"format": "ipynb", "parts": parts}


def udf_definition(wheel: Path | None = None) -> dict:
    """`definition.json`, `resources/functions.json`, `function_app.py` and `.platform`
    (T-102; `README.md` is repo documentation, not part of the item) -- confirmed against
    a live tenant (2026-11-01) to be the real required parts, not the `.platform` plus
    Python-file-only payload T-102 originally shipped. The Manage Connections binding
    (alias `nexoracontroldb`, see `fabric/nexora_api/function_app.py`'s module
    docstring) isn't part of this payload -- it has to be added once, by hand, in the
    portal after this item exists, since connections aren't Git/definition-trackable
    content.

    A UDF has no Environment to attach (confirmed 2026-11-02: unlike a notebook,
    there's nothing in this item's definition shape that references nexora_env --
    `call`/`me` failed at invocation with a generic `NotFound` until this was fixed).
    The `app` package has to reach it as its own private library wheel instead
    (`privateLibraries/<wheel>.whl`, declared in `definition.json`'s
    `libraries.private`, per Microsoft's UserDataFunctionDefinition reference) --
    `wheel` is the same `nexora-*.whl` `deploy()` already builds for nexora_env."""
    parts = [p for p in _folder_parts(FABRIC_DIR / API_ITEM) if p["path"] != "README.md"]

    if wheel is not None:
        for part in parts:
            if part["path"] == "definition.json":
                payload = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
                payload.setdefault("libraries", {})["private"] = [{"name": wheel.name, "type": "WHEEL"}]
                part["payload"] = base64.b64encode(
                    (json.dumps(payload, indent=2) + "\n").encode("utf-8")
                ).decode("ascii")
                break
        parts.append(
            {
                "path": f"privateLibraries/{wheel.name}",
                "payload": base64.b64encode(wheel.read_bytes()).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        )

    return {"parts": parts}


def variable_library_definition(env: str, overrides: dict[str, str]) -> dict:
    """The nexora_settings definition, with `overrides` (the ids this run resolved)
    written into `env`'s value set, on top of whatever is already committed there."""
    folder = FABRIC_DIR / f"{SETTINGS_ITEM}.VariableLibrary"
    parts = _folder_parts(folder)

    value_set_path = f"valueSets/{env}.json"
    value_set_file = folder / "valueSets" / f"{env}.json"
    value_set = json.loads(value_set_file.read_text(encoding="utf-8")) if value_set_file.is_file() else {}

    merged = {o["name"]: o["value"] for o in value_set.get("variableOverrides", [])}
    merged.update(overrides)
    value_set["variableOverrides"] = [{"name": name, "value": value} for name, value in sorted(merged.items())]

    encoded = base64.b64encode(json.dumps(value_set, indent=2, sort_keys=True).encode("utf-8")).decode("ascii")
    for part in parts:
        if part["path"] == value_set_path:
            part["payload"] = encoded
            break
    else:
        parts.append({"path": value_set_path, "payload": encoded, "payloadType": "InlineBase64"})

    return {"parts": parts}


def resolve_target_workspace(
    write_client: FabricItemsClient, read_client: FabricApiClient, create_name: str | None
) -> dict:
    if create_name:
        matches = [w for w in read_client.list_workspaces() if w["display_name"] == create_name]
        if matches:
            return write_client.get_workspace(matches[0]["id"])

        created = write_client.create_workspace(create_name)
        return write_client.get_workspace(created["id"])

    home_id = get_optional_setting("NEXORA_HOME_WORKSPACE_ID")
    if not home_id:
        raise BadArguments("NEXORA_HOME_WORKSPACE_ID is not set; pass --create NAME the first time")

    return write_client.get_workspace(home_id)


def _previous_wheel_hash() -> str | None:
    if not BUILD_MANIFEST.is_file():
        return None
    try:
        return json.loads(BUILD_MANIFEST.read_text(encoding="utf-8")).get("wheel_hash")
    except (OSError, ValueError):
        return None


def _sql_database_endpoint(item: dict | None) -> tuple[str | None, str | None]:
    """Confirmed against a live tenant (2026-11-01): `item` must come from
    FabricItemsClient.get_sql_database (the dedicated sqlDatabases/{id} endpoint),
    not get_item_by_name's generic items list, which never carries `properties` at
    all. `serverFqdn` already includes the `,1433` port suffix."""
    if not item:
        return None, None
    properties = item.get("properties") or {}
    server = properties.get("serverFqdn") or properties.get("connectionString")
    database = properties.get("databaseName") or item.get("displayName")
    return server, database


def deploy(client: FabricItemsClient, workspace_id: str, env: str, dry_run: bool) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    warnings: list[str] = []

    def record(name: str, operation: str) -> None:
        results.append((name, operation))

    previous_wheel_hash = _previous_wheel_hash()
    build: dict | None = None
    if not dry_run:
        try:
            build = package()
        except PackageError as error:
            raise DeployError(str(error)) from None

    env_result = client.upsert_item(workspace_id, ENVIRONMENT_ITEM, "Environment", dry_run=dry_run)
    record(ENVIRONMENT_ITEM, env_result["operation"])

    # A failure here is caught and reported rather than blocking every other item, since
    # the UDF definition shape needed more live-tenant fixing than the other item types.
    try:
        wheel = build["wheel"] if build else None
        api_result = client.upsert_user_data_function(workspace_id, API_ITEM, udf_definition(wheel), dry_run=dry_run)
        record(API_ITEM, api_result["operation"])
    except (FabricOperationFailed, NameConflict) as error:
        record(API_ITEM, f"FAILED: {error}")
        warnings.append(f"{API_ITEM} was not deployed: {error}")

    notebook_result = client.upsert_item(
        workspace_id,
        STAGE_RUNNER_ITEM,
        "Notebook",
        definition=notebook_definition(workspace_id, env_result["item_id"]),
        dry_run=dry_run,
    )
    record(STAGE_RUNNER_ITEM, notebook_result["operation"])

    db_result = client.upsert_item(workspace_id, CONTROL_DB_ITEM, "SQLDatabase", dry_run=dry_run)
    record(CONTROL_DB_ITEM, db_result["operation"])

    lakehouse_result = client.upsert_item(workspace_id, CONTROL_LAKEHOUSE_ITEM, "Lakehouse", dry_run=dry_run)
    record(CONTROL_LAKEHOUSE_ITEM, lakehouse_result["operation"])

    eventhouse_result = client.upsert_item(workspace_id, TELEMETRY_ITEM, "Eventhouse", dry_run=dry_run)
    record(TELEMETRY_ITEM, eventhouse_result["operation"])

    overrides = {
        "NEXORA_HOME_WORKSPACE_ID": workspace_id,
        "NEXORA_CONTROL_LAKEHOUSE_ID": lakehouse_result["item_id"] or "",
        "NEXORA_STAGE_RUNNER_ID": notebook_result["item_id"] or "",
        # Confirmed against a live tenant (2026-11-02): ARTIFACT_STORE defaults to "local"
        # (app/store/factory.py), which inside a job means the Spark driver's own ephemeral
        # disk -- invisible to the UDF reading the same run's files back. A home workspace
        # always means the control Lakehouse exists, so this is always "onelake" here.
        "ARTIFACT_STORE": "onelake",
    }

    db_item = client.get_sql_database(workspace_id, db_result["item_id"]) if not dry_run and db_result["item_id"] else None
    server, database = _sql_database_endpoint(db_item)
    if server and database:
        overrides["FABRIC_CONTROL_DB_SERVER"] = server
        overrides["FABRIC_CONTROL_DB_NAME"] = database
    elif not dry_run:
        warnings.append(
            f"Could not read {CONTROL_DB_ITEM}'s connection details; "
            "set FABRIC_CONTROL_DB_SERVER and FABRIC_CONTROL_DB_NAME in nexora_settings by hand."
        )

    try:
        settings_result = client.upsert_item(
            workspace_id,
            SETTINGS_ITEM,
            "VariableLibrary",
            definition=variable_library_definition(env, overrides),
            dry_run=dry_run,
        )
        record(SETTINGS_ITEM, settings_result["operation"])
        if not dry_run and settings_result["item_id"]:
            client.set_active_value_set(workspace_id, settings_result["item_id"], env)
    except (FabricOperationFailed, NameConflict) as error:
        record(SETTINGS_ITEM, f"FAILED: {error}")
        warnings.append(f"{SETTINGS_ITEM} was not deployed: {error}")

    if not dry_run:
        if build["manifest"] and json.loads(build["manifest"].read_text(encoding="utf-8"))["wheel_hash"] != previous_wheel_hash:
            client.upload_environment_library(workspace_id, env_result["item_id"], build["wheel"])
            client.publish_environment(workspace_id, env_result["item_id"])
            record(f"{ENVIRONMENT_ITEM} (wheel)", "updated")
        else:
            record(f"{ENVIRONMENT_ITEM} (wheel)", "unchanged")

        for config_file in sorted(CONFIG_DIR.glob("*.yaml")):
            write_file(
                workspace_id,
                lakehouse_result["item_id"],
                f"config/{config_file.name}",
                config_file.read_bytes(),
            )
        record(f"{CONTROL_LAKEHOUSE_ITEM} (config)", "updated")

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.deploy_home",
        description="Create or update the Nexora home workspace's items.",
    )
    parser.add_argument("--env", required=True, choices=["dev", "test", "prod"])
    parser.add_argument("--create", metavar="NAME", help="create the home workspace, the first time")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from app.auth import FABRIC_SCOPE, get_token

    write_client = FabricItemsClient(token_provider=lambda: get_token(FABRIC_SCOPE), allow_any_workspace=True)
    read_client = FabricApiClient(token_provider=lambda: get_token(FABRIC_SCOPE))

    try:
        workspace = resolve_target_workspace(write_client, read_client, args.create)
    except BadArguments as error:
        print(error, file=sys.stderr)
        return 2

    try:
        if not workspace.get("capacity_id"):
            raise DeployError(f"assign a Fabric capacity to {workspace['display_name']}")

        results = deploy(write_client, workspace["id"], args.env, args.dry_run)
    except NameConflict as error:
        print(f"NAME_CONFLICT: {error}", file=sys.stderr)
        return 1
    except (DeployError, FabricOperationFailed, FabricTimeout, FabricUpstreamError) as error:
        print(error, file=sys.stderr)
        return 1

    for name, operation in results:
        print(f"{name}: {operation}")

    return 1 if any(operation.startswith("FAILED") for _, operation in results) else 0


if __name__ == "__main__":
    sys.exit(main())
