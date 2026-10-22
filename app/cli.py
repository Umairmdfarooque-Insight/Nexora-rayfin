"""Nexora command line. Usage: python -m app.cli runs|contract|plan|report|model|deploy <command> ..."""

import argparse
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.contracts import business_contract
from app.store.base import (
    IllegalTransition,
    RunCorrupt,
    RunNotFound,
    StaleArtifacts,
    StoreUnavailable,
)
from app.store.factory import get_run_store
from app.workflow.states import RunState

RESUME = "RESUME"


@contextmanager
def _resolved_run_folder(path_str: str):
    """`path` as a real local folder when one exists; otherwise (ARTIFACT_STORE=
    onelake, no local mount) downloads the run's files to a temporary folder
    first (T-101 FR-7). `run_id` is the folder's last path component."""
    path = Path(path_str)
    if path.is_dir():
        yield path, None
        return

    from app.store.factory import get_artifact_store
    from app.store.pull import pull_run

    run_id = path.name
    store = get_artifact_store()
    local = store.local_path(run_id, "")
    if local is not None and local.is_dir():
        yield local, None
        return

    with tempfile.TemporaryDirectory(prefix="nexora-") as tmp:
        # The folder's own basename must be the run_id (callers reconstruct run_id
        # from it), so pull into a run_id-named subfolder of the temp directory.
        dest = Path(tmp) / run_id
        pull_run(store, run_id, dest)
        yield dest, run_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)

    runs = groups.add_parser("runs", help="Create and inspect runs")
    commands = runs.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a run and print its run_id")
    create.add_argument("--prompt", required=True)
    create.add_argument("--subject-area")
    create.add_argument("--output", action="append", default=[], dest="outputs")
    create.add_argument("--constraint", action="append", default=[], dest="constraints")

    list_ = commands.add_parser("list", help="List runs, newest first")
    list_.add_argument("--state")

    show = commands.add_parser("show", help="Print a run's manifest")
    show.add_argument("run_id")

    transition = commands.add_parser("transition", help="Move a run to a new state")
    transition.add_argument("run_id")
    transition.add_argument(
        "state", help=f"A run state, or {RESUME} to leave NEEDS_INPUT"
    )
    transition.add_argument("--reason", default="Manual transition")

    pull = commands.add_parser(
        "pull", help="Copy a run's files from the artifact store to a local folder"
    )
    pull.add_argument("run_id")
    pull.add_argument("--to", dest="to", help="Destination folder (default: runs/<run_id>)")

    contract = groups.add_parser("contract", help="Validate BusinessContract files")
    contract_cmds = contract.add_subparsers(dest="command", required=True)
    validate = contract_cmds.add_parser("validate", help="Check a BusinessContract.yaml file")
    validate.add_argument("file")
    schema = contract_cmds.add_parser("schema", help="Print the BusinessContract JSON Schema")
    schema.add_argument("--output", help="Write the schema to this file (UTF-8, LF) instead of printing")

    plan = groups.add_parser("plan", help="Validate a run's ModelSpec.yaml and ExecutionPlan.yaml")
    plan_cmds = plan.add_subparsers(dest="command", required=True)
    plan_validate = plan_cmds.add_parser("validate", help="Check the plan files in a run folder (runs/<run_id>)")
    plan_validate.add_argument("path")

    report = groups.add_parser("report", help="Validate a run's ReportPlan.json")
    report_cmds = report.add_subparsers(dest="command", required=True)
    report_validate = report_cmds.add_parser("validate", help="Check ReportPlan.json against ModelSpec.yaml in a run folder (runs/<run_id>)")
    report_validate.add_argument("path")

    deploy = groups.add_parser("deploy", help="Deploy generated artifacts to the sandbox workspace")
    deploy_cmds = deploy.add_subparsers(dest="command", required=True)
    deploy_model = deploy_cmds.add_parser("model", help="Create or update a semantic model from a TMDL folder")
    deploy_model.add_argument("tmdl_dir")
    deploy_model.add_argument("--name", required=True, help="Display name of the semantic model")
    deploy_model.add_argument("--workspace-id", help="Target workspace (default: SANDBOX_WORKSPACE_ID); anything but the sandbox is refused")

    model = groups.add_parser("model", help="Generate a run's semantic model files")
    model_cmds = model.add_subparsers(dest="command", required=True)
    model_generate = model_cmds.add_parser("generate", help="Write the next TMDL version of a run folder (runs/<run_id>)")
    model_generate.add_argument("path")
    return parser


def _parse_state(value: str) -> RunState:
    try:
        return RunState(value.upper())
    except ValueError:
        valid = ", ".join(s.value for s in RunState)
        raise ValueError(f"Invalid state: {value!r}. Valid states: {valid}") from None


def _contract_command(args) -> int:
    if args.command == "schema":
        text = json.dumps(business_contract.json_schema(), indent=2) + "\n"
        if args.output:
            # Written as bytes: PowerShell's ">" would save UTF-16.
            with open(args.output, "wb") as handle:
                handle.write(text.encode("utf-8"))
        else:
            print(text, end="")
        return 0
    try:
        business_contract.load(args.file)
    except FileNotFoundError:
        print(f"File not found: {args.file}", file=sys.stderr)
        return 2
    except business_contract.ContractInvalid as exc:
        print("INVALID")
        for issue in exc.issues:
            print(issue)
        return 1
    print("VALID")
    return 0


def _plan_command(args) -> int:
    from pydantic import ValidationError

    from app.contracts.business_contract import ContractInvalid, load as load_contract
    from app.contracts.feasibility_report import FeasibilityReport
    from app.contracts.metadata_inventory import MetadataInventory
    from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, to_data
    from app.discovery.plan_validation import validate_plan
    from app.workflow.harvest_stage import canonical_hash

    with _resolved_run_folder(args.path) as (folder, _run_id):
        for name in ("ModelSpec.yaml", "ExecutionPlan.yaml"):
            if not (folder / name).is_file():
                print(f"File not found: {folder / name}", file=sys.stderr)
                return 2
        try:
            model = load_yaml(ModelSpec, folder / "ModelSpec.yaml")
            plan = load_yaml(ExecutionPlan, folder / "ExecutionPlan.yaml")
        except (ValidationError, ValueError) as exc:
            print("INVALID")
            if isinstance(exc, ValidationError):
                for e in exc.errors():
                    print(f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
            else:
                print(f"  {exc}")
            return 1

        inventory = feasibility = contract = None
        if (folder / "MetadataInventory.json").is_file():
            inventory = MetadataInventory.model_validate_json((folder / "MetadataInventory.json").read_text(encoding="utf-8"))
        if (folder / "Feasibility.json").is_file():
            feasibility = FeasibilityReport.model_validate_json((folder / "Feasibility.json").read_text(encoding="utf-8"))
        if (folder / "BusinessContract.yaml").is_file():
            try:
                contract = load_contract(folder / "BusinessContract.yaml")
            except ContractInvalid:
                contract = None
        result = validate_plan(model, plan, inventory, feasibility, contract, canonical_hash(to_data(model)))
        failed = [c for c in result.checks if c.status == "fail"]
        for c in result.checks:
            if c.status == "warning":
                for path in c.evidence.get("paths", [c.description]):
                    print(f"  warning {c.id}: {path}")
        if failed:
            print("INVALID")
            for c in failed:
                for path in c.evidence.get("paths", [c.description]):
                    print(f"  {c.id}: {path}")
            return 1
        print("VALID")
        return 0


def _report_command(args) -> int:
    import json

    from pydantic import ValidationError

    from app.contracts.model_plan import ModelSpec, load_yaml
    from app.report.plan_validation import validate_report_plan

    with _resolved_run_folder(args.path) as (folder, _run_id):
        for name in ("ModelSpec.yaml", "ReportPlan.json"):
            if not (folder / name).is_file():
                print(f"File not found: {folder / name}", file=sys.stderr)
                return 2
        try:
            spec = load_yaml(ModelSpec, folder / "ModelSpec.yaml")
            data = json.loads((folder / "ReportPlan.json").read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            print(f"Could not read the files: {exc}", file=sys.stderr)
            return 2
        _, result = validate_report_plan(data, spec)
        if result.passed:
            print("VALID")
            return 0
        print("INVALID")
        for c in result.checks:
            if c.status == "fail":
                for path in c.evidence.get("paths", [c.description]):
                    print(f"  {c.id}: {path}")
        return 1


def _model_command(args) -> int:
    from app.model.tmdl_generator import ModelGenerationError
    from app.workflow.clarification import NotFound, StaleVersion
    from app.workflow.harvest_stage import ScopeMissing
    from app.workflow.model_stage import ModelSpecInvalid, write_model_version

    path = Path(args.path)
    with _resolved_run_folder(args.path) as (folder, downloaded_run_id):
        if not folder.is_dir():
            print(f"Run folder not found: {path}", file=sys.stderr)
            return 2
        if not (folder / "ModelSpec.yaml").is_file():
            print(f"File not found: {folder / 'ModelSpec.yaml'}", file=sys.stderr)
            return 2
        try:
            version, files = write_model_version(get_run_store(), folder.resolve().parent, folder.name)
        except ModelSpecInvalid as exc:
            print("INVALID")
            for check in exc.failed:
                for path2 in check.evidence.get("paths", [check.description]):
                    print(f"  {check.id}: {path2}")
            return 1
        except (NotFound, StaleVersion, ScopeMissing, ModelGenerationError, RunNotFound) as exc:
            print(exc, file=sys.stderr)
            return 1
        if downloaded_run_id:
            # The folder was a temporary download (ARTIFACT_STORE=onelake with no
            # local mount): upload the new version back into the store.
            from app.store.factory import get_artifact_store

            version_dir = folder / "model" / f"v{version}"
            get_artifact_store().copy_tree(downloaded_run_id, version_dir, f"model/v{version}")
    print(f"v{version}: {len(files)} files in {folder / 'model' / f'v{version}'}")
    for name in files:
        print(f"  {name}")
    return 0


def _deploy_command(args) -> int:
    from pathlib import Path

    from app.compiler.critical_terms import ConfigError
    from app.connectors.fabric_api import FabricError
    from app.deploy.fabric_items import (
        FabricItemsClient,
        FabricOperationFailed,
        InvalidDefinition,
        NameConflict,
        TargetNotAllowed,
        get_sandbox_workspace_id,
    )

    workspace_id = args.workspace_id or get_sandbox_workspace_id()
    if not workspace_id:
        print("SANDBOX_WORKSPACE_ID is not set. Check your .env file.", file=sys.stderr)
        return 2
    try:
        result = FabricItemsClient().upsert_semantic_model(workspace_id, args.name, Path(args.tmdl_dir))
    except InvalidDefinition as exc:
        print(exc, file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except TargetNotAllowed as exc:
        print(f"TARGET_NOT_ALLOWED: {exc}", file=sys.stderr)
        return 1
    except NameConflict as exc:
        print(f"NAME_CONFLICT: {exc}", file=sys.stderr)
        return 1
    except FabricError as exc:
        print(f"UPSTREAM_ERROR: {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"Fabric request ID: {exc.request_id}", file=sys.stderr)
        if isinstance(exc, FabricOperationFailed) and exc.details:
            print(json.dumps(exc.details, indent=2), file=sys.stderr)
        return 1
    print(f"item_id: {result['item_id']}")
    print(f"operation: {result['operation']}")
    return 0


def _run_command(args) -> int:
    # Validate arguments before touching the store.
    if args.command == "list" and args.state:
        state_filter = _parse_state(args.state)
    if args.command == "transition":
        target = None if args.state.upper() == RESUME else _parse_state(args.state)

    store = get_run_store()

    if args.command == "create":
        run = store.create(
            args.prompt, args.subject_area, args.outputs, args.constraints
        )
        print(run.run_id)
    elif args.command == "list":
        runs = store.list(state_filter if args.state else None)
        print(f"{'run_id':<36}  {'state':<20}  {'created_at':<20}  prompt")
        for run in runs:
            prompt = " ".join(run.prompt.split())[:40]
            print(
                f"{run.run_id:<36}  {run.state.value:<20}  {run.created_at:<20}  {prompt}"
            )
    elif args.command == "show":
        print(store.get(args.run_id).to_json())
    elif args.command == "transition":
        run = store.transition(args.run_id, target, args.reason, actor="cli")
        print(f"{run.run_id}: {run.state.value}")
    elif args.command == "pull":
        from app.store.factory import get_artifact_store
        from app.store.pull import pull_run

        store.get(args.run_id)  # RunNotFound for an unknown run
        destination = Path(args.to) if args.to else Path("runs") / args.run_id
        count = pull_run(get_artifact_store(), args.run_id, destination)
        print(f"{count} files pulled into {destination}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # state messages contain "→"

    args = _build_parser().parse_args(argv)  # bad arguments exit 2
    if args.group == "contract":
        return _contract_command(args)
    if args.group == "plan":
        return _plan_command(args)
    if args.group == "report":
        return _report_command(args)
    if args.group == "deploy":
        return _deploy_command(args)
    if args.group == "model":
        return _model_command(args)
    try:
        return _run_command(args)
    except ValueError as exc:  # bad prompt, bad state name, bad RUN_STORE
        print(exc, file=sys.stderr)
        return 2
    except (RunNotFound, IllegalTransition, StaleArtifacts, RunCorrupt, StoreUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
