"""The feasibility stage service (T-402): check the run, have the model extract each KPI's needs,
check them against SourceResolution in code, save Feasibility.json and, for any data gap, move
the run to NEEDS_INPUT.

A plain function, not a graph: the only loop is one bounded retry after unusable model output,
which is not a stage graph in CONTRACTS.md#stage-graphs. The state changes only through the
store, and only to NEEDS_INPUT.
"""

import json
from pathlib import Path

from app.contracts.business_contract import FeasibilityStatus
from app.contracts.feasibility_report import SCHEMA_VERSION, FeasibilityReport
from app.contracts.source_resolution import SourceResolution
from app.discovery.feasibility import (
    SYSTEM_PROMPT,
    available_columns,
    build_payload,
    evaluate,
    llm_schema,
    parse_needs,
)
from app.discovery.source_resolver import SourceLlm
from app.llm.errors import LlmInvalidOutput
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import ARTIFACT as CONTRACT_ARTIFACT
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.harvest_stage import canonical_hash, load_inventory, write_json
from app.workflow.inventory_review import raise_data_gaps
from app.workflow.llm_cache import request_key, save_answer, saved_answer
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.stale import assert_fresh
from app.workflow.source_stage import ARTIFACT as RESOLUTION_ARTIFACT
from app.workflow.source_stage import CURRENT as RESOLUTION_FILE
from app.workflow.states import RunState

ARTIFACT = "Feasibility"
CURRENT = "Feasibility.json"
CACHE = "feasibility_suggestions.json"
FEASIBILITY_STATES = (RunState.DATA_PROFILED,)
LLM_ATTEMPTS = 2


def descriptions_wanted(kpis: list, only: set[str] | None) -> bool:
    """True if a KPI that was asked for (all of them, with no restriction) is a data gap."""
    return any(k.status is FeasibilityStatus.DATA_GAP and (only is None or k.kpi.casefold() in only) for k in kpis)


def load_resolution(runs_root: Path, run_id: str) -> SourceResolution:
    path = Path(runs_root) / run_id / RESOLUTION_FILE
    if not path.is_file():
        raise NotFound("Sources haven't been resolved for this run yet: POST /runs/{id}/plan/sources first")
    return SourceResolution.model_validate_json(path.read_text(encoding="utf-8"))


def _needs(llm: SourceLlm, run_dir: Path, user: str, contract):
    key = request_key(SYSTEM_PROMPT, user)
    raw = saved_answer(run_dir, CACHE, key)
    problem = ""
    for _ in range(1 if raw is not None else LLM_ATTEMPTS):
        if raw is None:
            ask = user if not problem else f"{user}\n\nYour previous answer was unusable ({problem}). Answer again with valid JSON."
            raw = llm(SYSTEM_PROMPT, ask, llm_schema())
        try:
            needs = parse_needs(raw, contract)
        except LlmInvalidOutput as exc:
            problem, raw = str(exc), None
            continue
        save_answer(run_dir, CACHE, key, raw)  # unusable output isn't kept
        return needs
    raise LlmInvalidOutput(problem)


def check_feasibility(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    llm: SourceLlm,
    actor: str = "system",
    restrict: set[str] | None = None,
) -> tuple[FeasibilityReport, Run]:
    """Check every KPI of the contract against the run's SourceResolution. `restrict` (T-701) names
    the KPIs to check again (`kpi:<name>`); the other KPIs keep their saved result."""
    run = store.get(run_id)
    if run.state not in FEASIBILITY_STATES:
        raise WrongState("Checking feasibility", run.state, FEASIBILITY_STATES)
    assert_fresh(run.artifacts, (RESOLUTION_ARTIFACT,), "Checking feasibility")
    resolution = load_resolution(runs_root, run_id)
    inventory = load_inventory(runs_root, run_id)
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")

    run_dir = Path(runs_root) / run_id
    columns = available_columns(resolution, inventory)
    needs = {}
    if contract.kpis:
        user = json.dumps(build_payload(contract, columns), sort_keys=True)
        needs = _needs(llm, run_dir, user, contract)
    kpis = evaluate(contract, resolution, columns, needs)
    only = {o[4:].casefold() for o in restrict if o.startswith("kpi:")} if restrict is not None else None
    if only is not None and (run_dir / CURRENT).is_file():
        saved = {k.kpi.casefold(): k for k in FeasibilityReport.model_validate_json((run_dir / CURRENT).read_text(encoding="utf-8")).kpis}
        kpis = [k if k.kpi.casefold() in only else saved.get(k.kpi.casefold(), k) for k in kpis]

    based_on = {RESOLUTION_ARTIFACT: canonical_hash(resolution.to_dict())}
    if CONTRACT_ARTIFACT in run.artifacts:
        based_on[CONTRACT_ARTIFACT] = run.artifacts[CONTRACT_ARTIFACT].hash
    gaps = any(k.status is FeasibilityStatus.DATA_GAP for k in kpis)

    current = run_dir / CURRENT
    previous = run.artifacts.get(ARTIFACT)
    report = None
    version = 1
    if previous is not None and current.is_file():
        old = FeasibilityReport.model_validate_json(current.read_text(encoding="utf-8"))
        if old.based_on == based_on and old.kpis == kpis:
            report = old
        version = max(old.version, previous.version) + 1
    if report is None:
        report = FeasibilityReport(
            schema_version=SCHEMA_VERSION, run_id=run_id, version=version, based_on=based_on, kpis=kpis
        )
        data = report.to_dict()
        write_json(run_dir / "feasibility" / f"Feasibility.v{version}.json", data)
        write_json(current, data)
        run.artifacts[ARTIFACT] = ArtifactRef(
            path=CURRENT,
            version=version,
            hash=canonical_hash(data),
            status="data_gap" if gaps else "draft",
            based_on=based_on,
            updated_at=utc_now(),
        )
        store.save(run)

    if gaps and descriptions_wanted(kpis, only):
        descriptions = [
            f"KPI {k.kpi!r} can't be calculated from the available data: "
            + "; ".join(f"{m.need} ({m.reason})" for m in k.missing)
            for k in kpis
            if k.status is FeasibilityStatus.DATA_GAP and (only is None or k.kpi.casefold() in only)
        ]
        run = raise_data_gaps(store, runs_root, run_id, descriptions, actor)
    else:
        run = store.get(run_id)
    return report, run
