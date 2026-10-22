"""The measure stage (T-503): DAX for every ModelSpec measure, then a new TMDL version.

For each measure the `dax_measure` graph asks the model for DAX and checks it in code (see
`model/dax_generator.py`). Accepted DAX and the approved definition (as the description) go into a
new version of ModelSpec.yaml, then `write_model_version` writes the next TMDL folder. A rejected
measure keeps whatever DAX it had and never gets invented DAX. One measure's failure (rejection,
timeout, unusable output) doesn't stop the others. The run state never changes, and the graph
never touches the store: this service does.

The model's accepted answers are kept in `dax_suggestions.json`, keyed by a hash of the request,
so unchanged inputs give the same DAX and no new version.
"""

from pathlib import Path

from app.contracts.model_plan import ModelSpec, load_yaml, save_yaml, to_data
from app.llm.errors import LlmTimeout, LlmUpstreamError, PromptRejected
from app.model.dax_generator import (
    SYSTEM_PROMPT,
    DaxRejected,
    LlmCall,
    approved_description,
    propose_measure_dax,
)
from app.store.base import RunStore
from app.workflow.clarification import NotFound, StaleVersion
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.harvest_stage import canonical_hash
from app.workflow.llm_cache import CachedLlm, read_answers, write_answers
from app.workflow.model_stage import TMDL_ARTIFACT, write_model_version
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.run import utc_now
from app.workflow.states import RunState

MEASURE_STATES = (RunState.MODEL_GENERATING,)
CACHE = "dax_suggestions.json"


def add_measures(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    llm: LlmCall,
    only: set[str] | None = None,
    expect_only: set[str] | None = None,
) -> tuple[list[dict], int]:
    """`POST /runs/{id}/model/measures`: returns the per-measure results and the TMDL version.

    `only` (T-701, targeted regeneration) limits the work to the measures with these names; the others
    are not touched. `expect_only` is passed on to `write_model_version` (its scope guard)."""
    run = store.get(run_id)
    if run.state not in MEASURE_STATES:
        raise WrongState("Generating measures", run.state, MEASURE_STATES)
    model_ref = run.artifacts.get(MODEL_ARTIFACT)
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    run_dir = Path(runs_root) / run_id
    if model_ref is None or tmdl_ref is None or not (run_dir / MODEL_FILE).is_file():
        raise NotFound("The model hasn't been generated yet: POST /runs/{id}/model/generate first")
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")
    spec = load_yaml(ModelSpec, run_dir / MODEL_FILE)
    spec_hash = canonical_hash(to_data(spec))
    if spec_hash != model_ref.hash:
        raise StaleVersion(model_ref.hash, spec_hash, "model spec")

    kpis = {k.name.casefold(): k for k in contract.kpis}
    cache = read_answers(run_dir, CACHE)
    accepted_answers: dict[str, str] = {}
    results: list[dict] = []
    changed = False
    for measure in sorted(spec.measures, key=lambda m: m.name):
        if only is not None and measure.name not in only:
            continue
        kpi = kpis.get(measure.kpi.casefold())
        if kpi is None:
            results.append({"name": measure.name, "dax": measure.dax, "status": "rejected", "reason": f"KPI {measure.kpi!r} is not in the approved contract"})
            continue
        cached = CachedLlm(llm, cache)
        try:
            proposal = propose_measure_dax(kpi, spec, cached, measure.name)
        except DaxRejected as exc:
            results.append({"name": measure.name, "dax": measure.dax, "status": "rejected", "reason": exc.reason})
            continue
        except (LlmTimeout, LlmUpstreamError, PromptRejected) as exc:
            results.append({"name": measure.name, "dax": measure.dax, "status": "rejected", "reason": f"the model call failed: {exc}"})
            continue
        accepted_answers[cached.last_key] = cached.last_raw
        description = approved_description(kpi)
        if measure.dax != proposal.dax or measure.description != description:
            measure.dax, measure.description = proposal.dax, description
            changed = True
        results.append({"name": measure.name, "dax": proposal.dax, "status": "accepted"})

    if accepted_answers:
        write_answers(run_dir, CACHE, {**cache, **accepted_answers})
    if not changed:
        return results, tmdl_ref.version

    spec.version += 1
    save_yaml(spec, run_dir / MODEL_FILE)
    save_yaml(spec, run_dir / "plans" / f"ModelSpec.v{spec.version}.yaml")
    run = store.get(run_id)
    model_ref = run.artifacts[MODEL_ARTIFACT]
    model_ref.version, model_ref.hash, model_ref.updated_at = spec.version, canonical_hash(to_data(spec)), utc_now()
    store.save(run)  # status stays as it was; the DAX is approved with the model at G4
    version, _ = write_model_version(store, runs_root, run_id, expect_only)
    return results, version
