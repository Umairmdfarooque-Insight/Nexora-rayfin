"""The report planning stage (T-601): ReportPlan.json from the approved contract and model.

A plain function with one bounded retry (inside `report/planner.py`). The model proposes pages and
visuals; `report/plan_validation.py` decides whether the plan stands. A question that no approved
measure answers is listed in `unanswered_questions`, a data gap is recorded and the run moves to
NEEDS_INPUT (`resume_state: MODEL_APPROVED`); no measure is invented. The state otherwise never
changes. The model's accepted answer is kept in `report_suggestions.json`, so unchanged inputs give
the same plan and version.
"""

from pathlib import Path

from app.contracts.model_plan import ModelSpec, load_yaml, to_data as spec_data
from app.contracts.report_plan import ReportPlan
from app.report.plan_validation import validate_report_plan
from app.report.planner import LlmCall, propose_report_plan
from app.store.base import RunStore
from app.workflow.clarification import NotFound, StaleVersion
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.gates import GateResult
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.inventory_review import raise_data_gaps
from app.workflow.llm_cache import CachedLlm, read_answers, write_answers
from app.workflow.model_stage import TMDL_ARTIFACT
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

ARTIFACT = "ReportPlan"
CURRENT = "ReportPlan.json"
CACHE = "report_suggestions.json"
PLAN_STATES = (RunState.MODEL_APPROVED,)
SEMANTIC_MODEL = "SemanticModel"  # the key in `based_on`: the approved model (its TMDLModel artifact hash)


def _based_on(run: Run) -> dict[str, str]:
    contract_ref = run.artifacts.get("BusinessContract")
    return {
        SEMANTIC_MODEL: run.artifacts[TMDL_ARTIFACT].hash,
        "BusinessContract": contract_ref.hash if contract_ref else "",
    }


def _load_spec(run: Run, runs_root: Path) -> ModelSpec:
    model_ref = run.artifacts.get(MODEL_ARTIFACT)
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    path = Path(runs_root) / run.run_id / MODEL_FILE
    if model_ref is None or tmdl_ref is None or not path.is_file():
        raise NotFound("The run has no approved model yet: generate and approve the semantic model first")
    spec = load_yaml(ModelSpec, path)
    spec_hash = canonical_hash(spec_data(spec))
    if spec_hash != model_ref.hash:
        raise StaleVersion(model_ref.hash, spec_hash, "model spec")
    return spec


def _saved(run_dir: Path) -> ReportPlan | None:
    path = run_dir / CURRENT
    if not path.is_file():
        return None
    try:
        return ReportPlan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None  # unreadable (hand-edited) file: the next plan replaces it as a new version


def _comparable(plan: ReportPlan) -> dict:
    return {**plan.to_dict(), "version": 0}


def generate_report_plan(
    store: RunStore, runs_root: Path, run_id: str, llm: LlmCall, actor: str = "system"
) -> tuple[ReportPlan, GateResult, Run]:
    """`POST /runs/{id}/report/plan`."""
    run = store.get(run_id)
    if run.state not in PLAN_STATES:
        raise WrongState("Planning the report", run.state, PLAN_STATES)
    assert_fresh(run.artifacts, (TMDL_ARTIFACT, MODEL_ARTIFACT), "Planning the report")
    spec = _load_spec(run, runs_root)
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")
    run_dir = Path(runs_root) / run_id

    cache = read_answers(run_dir, CACHE)
    cached = CachedLlm(llm, cache)
    plan = propose_report_plan(contract, spec, cached, run_id, run.subject_area, run.desired_outputs)
    plan.based_on = _based_on(run)
    _, result = validate_report_plan(plan, spec)  # also what the response reports
    if cached.last_key is not None and cached.last_key not in cache:
        write_answers(run_dir, CACHE, {**cache, cached.last_key: cached.last_raw})

    previous = _saved(run_dir)
    if previous is not None and _comparable(previous) == _comparable(plan):
        plan = previous
    else:
        plan.version = (previous.version + 1) if previous else _next_version(run_dir)
        data = plan.to_dict()
        write_json(run_dir / "reports" / f"ReportPlan.v{plan.version}.json", data)
        write_json(run_dir / CURRENT, data)
        run = store.get(run_id)
        run.artifacts[ARTIFACT] = ArtifactRef(
            path=CURRENT,
            version=plan.version,
            hash=canonical_hash(data),
            status="needs_input" if plan.unanswered_questions else "draft",
            based_on=plan.based_on,
            updated_at=utc_now(),
        )
        store.save(run)

    if plan.unanswered_questions:
        run = raise_data_gaps(
            store,
            runs_root,
            run_id,
            [
                f'Report question {u.question_id} can\'t be answered: {u.reason}. Return to the model or drop the question.'
                for u in plan.unanswered_questions
            ],
            actor,
            states=PLAN_STATES,
        )
    else:
        run = store.get(run_id)
    return plan, result, run


def _next_version(run_dir: Path) -> int:
    found = [int(p.stem.rsplit(".v", 1)[1]) for p in (run_dir / "reports").glob("ReportPlan.v*.json") if p.stem.rsplit(".v", 1)[1].isdigit()]
    return max(found, default=0) + 1


def read_report_plan(store: RunStore, runs_root: Path, run_id: str) -> tuple[dict, GateResult]:
    """`GET /runs/{id}/report/plan`: the saved file, checked again (so a hand edit shows up).
    Returns the raw data, because a hand-edited file may not match the schema."""
    import json

    run = store.get(run_id)
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        raise NotFound("No report plan has been generated for this run yet: POST /runs/{id}/report/plan first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NotFound(f"ReportPlan.json is not valid JSON: {exc}") from None
    spec = _load_spec(run, runs_root)
    _, result = validate_report_plan(data, spec)
    return data, result
