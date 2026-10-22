"""The source-resolution stage service (T-401): check the run, ask the model for candidates,
resolve them deterministically and save SourceResolution.json.

A plain function, not a graph: the stage has no loop (CONTRACTS.md#stage-graphs). It never
changes the run state. The model's answer is kept per input (a hash of the exact request), so
the same contract, inventory and glossary always give the same file.
"""

import json
from pathlib import Path

from app.contracts.source_resolution import SCHEMA_VERSION, SourceResolution
from app.discovery.glossary import load_glossary
from app.discovery.source_resolver import (
    SYSTEM_PROMPT,
    SourceLlm,
    Suggestions,
    build_payload,
    llm_schema,
    parse_suggestions,
    resolve,
)
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import ARTIFACT as CONTRACT_ARTIFACT
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.conformed import load_conformed
from app.workflow.harvest_stage import ARTIFACT as INVENTORY_ARTIFACT
from app.workflow.harvest_stage import canonical_hash, load_inventory, write_json
from app.workflow.inventory_review import approved_exception_ids
from app.workflow.llm_cache import request_key, save_answer, saved_answer
from app.workflow.run import ArtifactRef, utc_now
from app.workflow.states import RunState

ARTIFACT = "SourceResolution"
CURRENT = "SourceResolution.json"
CACHE = "source_suggestions.json"
SOURCE_STATES = (RunState.DATA_PROFILED,)



def load_saved_resolution(runs_root: Path, run_id: str) -> SourceResolution:
    """The saved SourceResolution.json (404 NOT_FOUND before /plan/sources)."""
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        raise NotFound("Sources haven't been resolved for this run yet: POST /runs/{id}/plan/sources first")
    return SourceResolution.model_validate_json(path.read_text(encoding="utf-8"))


def _restricted(old: SourceResolution, entities: list, kpi_fields: list, restrict: set[str]) -> tuple[list, list]:
    """The new entities and KPI fields only for the objects named in `restrict` (`kpi:<name>`,
    `entity:<name>`); everything else stays as it was in `old` (T-701, targeted regeneration)."""
    kpis = {o[4:].casefold() for o in restrict if o.startswith("kpi:")}
    ents = {o[7:].casefold() for o in restrict if o.startswith("entity:")}
    old_entities = {e.entity.casefold(): e for e in old.entities}
    old_kpis = {k.kpi.casefold(): k for k in old.kpi_fields}
    return (
        [e if e.entity.casefold() in ents else old_entities.get(e.entity.casefold(), e) for e in entities],
        [k if k.kpi.casefold() in kpis else old_kpis.get(k.kpi.casefold(), k) for k in kpi_fields],
    )


def resolve_sources(
    store: RunStore, runs_root: Path, run_id: str, llm: SourceLlm, restrict: set[str] | None = None
) -> SourceResolution:
    """Resolve the run's entities and KPI needs to tables and columns. `restrict` (T-701) names the
    objects to re-resolve (`kpi:<name>`, `entity:<name>`); the rest keeps the saved resolution."""
    run = store.get(run_id)
    if run.state not in SOURCE_STATES:
        raise WrongState("Resolving sources", run.state, SOURCE_STATES)
    inventory = load_inventory(runs_root, run_id)  # 404 NOT_FOUND before a harvest
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")
    glossary = load_glossary()  # CONFIG_ERROR before any model call
    conformed = load_conformed()
    exceptions = approved_exception_ids(store, runs_root, run_id)

    run_dir = Path(runs_root) / run_id
    suggestions = Suggestions()
    if contract.entities or contract.kpis:
        user = json.dumps(build_payload(contract, inventory, glossary.terms), sort_keys=True)
        key = request_key(SYSTEM_PROMPT, user)
        raw = saved_answer(run_dir, CACHE, key)
        if raw is None:
            raw = llm(SYSTEM_PROMPT, user, llm_schema())
        suggestions = parse_suggestions(raw, contract, inventory)  # unusable output isn't kept
        save_answer(run_dir, CACHE, key, raw)

    entities, kpi_fields = resolve(contract, inventory, glossary, conformed, exceptions, suggestions)
    if restrict is not None and (run_dir / CURRENT).is_file():
        entities, kpi_fields = _restricted(
            SourceResolution.model_validate_json((run_dir / CURRENT).read_text(encoding="utf-8")),
            entities, kpi_fields, restrict,
        )

    based_on = {
        name: run.artifacts[name].hash
        for name in (CONTRACT_ARTIFACT, INVENTORY_ARTIFACT)
        if name in run.artifacts
    }
    previous = run.artifacts.get(ARTIFACT)
    current = run_dir / CURRENT
    if previous is not None and current.is_file():
        old = SourceResolution.model_validate_json(current.read_text(encoding="utf-8"))
        same = (
            old.based_on == based_on
            and old.entities == entities
            and old.kpi_fields == kpi_fields
        )
        if same:
            return old
        version = max(old.version, previous.version) + 1
    else:
        version = 1

    result = SourceResolution(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        version=version,
        based_on=based_on,
        entities=entities,
        kpi_fields=kpi_fields,
    )
    data = result.to_dict()
    write_json(run_dir / "resolutions" / f"SourceResolution.v{version}.json", data)
    write_json(current, data)
    run.artifacts[ARTIFACT] = ArtifactRef(
        path=CURRENT,
        version=version,
        hash=canonical_hash(data),
        status="draft",
        based_on=based_on,
        updated_at=utc_now(),
    )
    store.save(run)
    return result
