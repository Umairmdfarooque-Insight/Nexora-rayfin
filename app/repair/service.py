"""The repair service (T-505, FR-016, FR-017): the smallest repair of a failed artifact, a bounded
number of attempts, escalation to the user.

`repair` checks the run, runs the `repair` graph once and then, and only then, registers the
artifacts and makes the run transition (one transition per call, whatever the number of attempts):
    passed                          MODEL_FAILED -> MODEL_GENERATING (awaiting review)
    user_input_only / limit_reached / no_progress
                                    MODEL_FAILED -> NEEDS_INPUT (resume_state MODEL_FAILED)
The generic part is the graph; `artifact` picks the adapter: "SemanticModel" (above) and "Report"
(T-603): REPORT_FAILED -> REPORT_GENERATING when passed, else NEEDS_INPUT (resume_state REPORT_FAILED);
a remaining model defect (a report query that fails on an object the ModelSpec has) goes to
MODEL_GENERATING instead.
"""

from pathlib import Path
from typing import Any, Literal

from app.contracts.model_plan import to_data
from app.contracts.validation_report import load_report
from app.graphs.base import run_graph
from app.graphs.repair import NODES_PER_ATTEMPT, build_repair_graph
from app.repair.model_adapter import ModelRepairAdapter
from app.repair.report_adapter import ReportRepairAdapter
from app.repair.models import RepairAttempt, RepairResult
from app.repair.policy import load_policy
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState
from app.workflow.harvest_stage import canonical_hash
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash
from app.workflow.plan_stage import MODEL_ARTIFACT
from app.workflow.run import utc_now
from app.workflow.states import RunState
from app.workflow.validate_stage import REPORT_FILE, Environment, register_report

REPAIR_STATES = {"SemanticModel": (RunState.MODEL_FAILED,), "Report": (RunState.REPORT_FAILED,)}


class NothingToRepair(Exception):
    """The run has no failed check to repair."""


class UnsupportedArtifact(Exception):
    def __init__(self, artifact: str):
        self.artifact = artifact
        super().__init__(f"Repair of {artifact!r} isn't available yet")


def summary(outcome: str, attempts: list[RepairAttempt], remaining: list[Any]) -> str:
    """The message the user gets when the run needs them."""
    lines = []
    for c in remaining[:5]:
        lines.append(f"[{c.classification}] {c.object}: {c.message}")
    more = f" (and {len(remaining) - 5} more)" if len(remaining) > 5 else ""
    reason = {
        "user_input_only": "These problems need your input and can't be repaired automatically",
        "no_progress": "Repair stopped because an attempt did not change the failures",
        "limit_reached": "Repair stopped at the attempt limit",
    }[outcome]
    return f"{reason} after {len(attempts)} attempt(s). Remaining: " + "; ".join(lines) + more


def repair(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    artifact: Literal["SemanticModel", "Report"] = "SemanticModel",
    *,
    llm: Any = None,
    items_client: Any,
    pbi: Any,
    sql_connector_factory: Any = None,
    recon_llm: Any = None,
    user_upn: str | None = None,
    actor: str = "system",
) -> RepairResult:
    """`POST /runs/{id}/model/repair` and `POST /runs/{id}/report/repair`."""
    if artifact not in REPAIR_STATES:
        raise UnsupportedArtifact(artifact)
    run = store.get(run_id)
    allowed = REPAIR_STATES[artifact]
    if run.state not in allowed:
        raise WrongState("Repair", run.state, allowed)
    if artifact == "Report":
        return repair_report(store, runs_root, run_id, items_client=items_client, pbi=pbi, actor=actor)
    run_dir = Path(runs_root) / run_id
    report = load_report(run_dir / REPORT_FILE, run_id)
    latest = report.latest("SemanticModel")
    failed = [c for c in latest.checks if c.status == "fail"] if latest else []
    if not failed:
        raise NothingToRepair("Nothing to repair: the latest validation has no failed check")
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    if tmdl_ref is None or not (run_dir / tmdl_ref.path).is_dir():
        raise NotFound("The model hasn't been generated yet: POST /runs/{id}/model/generate first")

    policy = load_policy()  # CONFIG_ERROR before anything is changed
    env = Environment(store, runs_root, run_id)
    adapter = ModelRepairAdapter(
        env, tmdl_ref.version, run_dir / tmdl_ref.path,
        items_client=items_client, pbi=pbi, sql_connector_factory=sql_connector_factory, recon_llm=recon_llm, user_upn=user_upn,
    )
    graph = build_repair_graph(llm)
    final = run_graph(
        graph,
        {
            "run_id": run_id, "artifact": artifact, "adapter": adapter, "policy": policy, "version": tmdl_ref.version,
            "errors": failed, "attempt": 0, "attempts": [], "seen_fingerprints": [],
        },
        run_id=run_id,
        stage="model.repair",
        recursion_limit=policy.max_attempts * NODES_PER_ATTEMPT + 10,
    )
    outcome = final["outcome"]
    attempts = [RepairAttempt(**a) for a in final.get("attempts", [])]

    # The graph wrote files only. Register what it kept, then make the single transition.
    if attempts and any(not a.discarded for a in attempts):
        run = store.get(run_id)
        folder = run_dir / "model" / f"v{adapter.version}"
        previous = run.artifacts[TMDL_ARTIFACT]
        run.artifacts[TMDL_ARTIFACT] = previous.model_copy(
            update={
                "path": f"model/v{adapter.version}", "version": adapter.version, "hash": folder_hash(folder),
                "status": "draft", "based_on": {MODEL_ARTIFACT: adapter.spec_hash()}, "updated_at": utc_now(),
            }
        )
        run.artifacts[MODEL_ARTIFACT] = run.artifacts[MODEL_ARTIFACT].model_copy(
            update={"version": adapter.committed.version, "hash": adapter.spec_hash(), "updated_at": utc_now()}
        )
        store.save(run)
        latest_entry = load_report(run_dir / REPORT_FILE, run_id).latest("SemanticModel")
        register_report(store, runs_root, run_id, latest_entry)

    message = None
    if outcome == "passed":
        run = store.transition(run_id, RunState.MODEL_GENERATING, f"Model repaired in {len(attempts)} attempt(s)", actor)
    else:
        remaining = adapter.classify(final["errors"], policy)
        message = summary(outcome, attempts, remaining)
        run = store.transition(run_id, RunState.NEEDS_INPUT, message[:400], actor)
    return RepairResult(attempts=attempts, final_state=run.state, outcome=outcome, message=message)


def repair_report(store: RunStore, runs_root: Path, run_id: str, *, items_client: Any, pbi: Any, actor: str = "system") -> RepairResult:
    """Repair a failed report: the visual or page of each report defect is regenerated from the plan."""
    from app.workflow.report_stage import ARTIFACT as PBIR, REPORT_DIR
    from app.workflow.report_validate_stage import ReportEnvironment, model_defects, route_model_defects

    run = store.get(run_id)
    run_dir = Path(runs_root) / run_id
    report = load_report(run_dir / REPORT_FILE, run_id)
    latest = report.latest("Report")
    failed = [c for c in latest.checks if c.status == "fail"] if latest else []
    if not failed:
        raise NothingToRepair("Nothing to repair: the latest report validation has no failed check")
    ref = run.artifacts.get(PBIR)
    if ref is None or not (run_dir / ref.path).is_dir():
        raise NotFound("The report hasn't been generated yet: POST /runs/{id}/report/generate first")

    policy = load_policy()  # CONFIG_ERROR before anything is changed
    env = ReportEnvironment(store, runs_root, run_id)
    adapter = ReportRepairAdapter(env, ref.version, run_dir / ref.path, items_client=items_client, pbi=pbi)
    final = run_graph(
        build_repair_graph(None),
        {
            "run_id": run_id, "artifact": "Report", "adapter": adapter, "policy": policy, "version": ref.version,
            "errors": failed, "attempt": 0, "attempts": [], "seen_fingerprints": [],
        },
        run_id=run_id,
        stage="report.repair",
        recursion_limit=policy.max_attempts * NODES_PER_ATTEMPT + 10,
    )
    outcome = final["outcome"]
    attempts = [RepairAttempt(**a) for a in final.get("attempts", [])]

    from app.report.pbir_generator import folder_digest

    if attempts and any(not a.discarded for a in attempts):
        run = store.get(run_id)
        folder = run_dir / REPORT_DIR / f"v{adapter.version}"
        run.artifacts[PBIR] = ref.model_copy(
            update={"path": f"{REPORT_DIR}/v{adapter.version}", "version": adapter.version, "hash": folder_digest(folder), "status": "draft", "updated_at": utc_now()}
        )
        store.save(run)
        register_report(store, runs_root, run_id, load_report(run_dir / REPORT_FILE, run_id).latest("Report"))

    message = None
    if outcome == "passed":
        run = store.transition(run_id, RunState.REPORT_GENERATING, f"Report repaired in {len(attempts)} attempt(s)", actor)
    else:
        latest_entry = load_report(run_dir / REPORT_FILE, run_id).latest("Report")
        if latest_entry is not None and model_defects(latest_entry):
            run = route_model_defects(store, runs_root, run_id, latest_entry, actor)
            message = "A model defect was found, so the run went back to the model: " + "; ".join(c.message for c in model_defects(latest_entry))
        else:
            remaining = adapter.classify(final["errors"], policy)
            message = summary(outcome, attempts, remaining)
            run = store.transition(run_id, RunState.NEEDS_INPUT, message[:400], actor)
    return RepairResult(attempts=attempts, final_state=run.state, outcome=outcome, message=message)
