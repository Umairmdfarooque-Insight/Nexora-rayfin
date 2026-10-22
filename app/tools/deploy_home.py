"""python -m app.tools.deploy_home --env dev|test|prod [--create NAME] [--dry-run]

Creates or updates the Nexora home workspace's items (CLAUDE.md "Target hosting",
T-005) from the definitions committed under `fabric/`. Deployment is allowed only
into a workspace whose name starts with "Nexora-": either the one in
NEXORA_HOME_WORKSPACE_ID, or, the first time, the one named with --create.

The exact Git/definition payload shapes for a few item types (UserDataFunctions,
VariableLibrary) are reconstructed from public docs, not yet confirmed against a
live tenant — see fabric-hosting.md's platform-limits table. Those items are
created with the simplest payload that should work; a user running this against a
real tenant for the first time is this task's own manual test (AC-2 to AC-6).
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


def notebook_definition() -> dict:
    return {"format": "ipynb", "parts": _folder_parts(FABRIC_DIR / f"{STAGE_RUNNER_ITEM}.Notebook")}


def udf_definition() -> dict:
    """`.platform` and `function-app.py` (T-102; `README.md` is repo documentation,
    not part of the item). Not yet confirmed against a live tenant: the Manage
    Connections binding (alias `nexora_control_db`, see
    `fabric/nexora_api/function-app.py`'s module docstring) isn't part of this
    payload -- it has to be added once, by hand, in the portal after this item
    exists, since connections aren't Git/definition-trackable content."""
    parts = [p for p in _folder_parts(FABRIC_DIR / API_ITEM) if p["path"] != "README.md"]
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
        if not create_name.startswith("Nexora-"):
            raise BadArguments(f"--create {create_name!r} must start with 'Nexora-'; nothing deployed")

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
    """Best-effort read of a SQLDatabase item's connection details. Not yet confirmed
    against a live tenant: if the property names below don't match, the two settings
    are left blank and a warning is printed, rather than guessing further."""
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

    env_result = client.upsert_item(workspace_id, ENVIRONMENT_ITEM, "Environment", dry_run=dry_run)
    record(ENVIRONMENT_ITEM, env_result["operation"])

    api_result = client.upsert_item(
        workspace_id, API_ITEM, "UserDataFunctions", definition=udf_definition(), dry_run=dry_run
    )
    record(API_ITEM, api_result["operation"])

    notebook_result = client.upsert_item(
        workspace_id, STAGE_RUNNER_ITEM, "Notebook", definition=notebook_definition(), dry_run=dry_run
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
    }

    db_item = client.get_item_by_name(workspace_id, CONTROL_DB_ITEM, "SQLDatabase") if not dry_run else None
    server, database = _sql_database_endpoint(db_item)
    if server and database:
        overrides["FABRIC_CONTROL_DB_SERVER"] = server
        overrides["FABRIC_CONTROL_DB_NAME"] = database
    elif not dry_run:
        warnings.append(
            f"Could not read {CONTROL_DB_ITEM}'s connection details; "
            "set FABRIC_CONTROL_DB_SERVER and FABRIC_CONTROL_DB_NAME in nexora_settings by hand."
        )

    settings_result = client.upsert_item(
        workspace_id,
        SETTINGS_ITEM,
        "VariableLibrary",
        definition=variable_library_definition(env, overrides),
        dry_run=dry_run,
    )
    record(SETTINGS_ITEM, settings_result["operation"])

    if not dry_run:
        previous_wheel_hash = _previous_wheel_hash()
        try:
            build = package()
        except PackageError as error:
            raise DeployError(str(error)) from None

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

        if not workspace["display_name"].startswith("Nexora-"):
            raise BadArguments(
                f"{workspace['display_name']!r} is not a Nexora- workspace; nothing deployed"
            )
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
