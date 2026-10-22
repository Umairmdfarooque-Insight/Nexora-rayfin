"""Impact analysis and stale marking (T-701, FR-020, business rules 2 and 6).

`return_run` sends an approved run back to requirement analysis, planning or the model and unlocks the
artifact of that stage. Once the changed artifact is approved again, `refresh_impact` compares the
version the run had when it was returned (the baseline in `returns.jsonl`) with the current one, at
object level, and follows the dependency map:

    BusinessContract -> SourceResolution, Feasibility, ExecutionPlan, ModelSpec
    ModelSpec        -> TMDLModel (a measure, a table file, a relationship file)
    TMDLModel        -> ReportPlan -> PbirReport (the visuals that use a changed measure or table)

Every affected artifact gets `status: stale` (its earlier status is kept in `Impact.json`) and the
objects inside it are listed. A stale artifact blocks the stages that build on it (`stale.assert_fresh`)
and a deployment. The analysis is deterministic: no model is involved, and an unreadable baseline file
makes the whole artifact stale rather than none of it.

Earlier versions are never deleted: the contract, plan, model and report versions the baseline names
stay on disk.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.report_plan import FieldRef, ReportPlan, Visual
from app.model.dax_generator import extract_references
from app.model.tmdl_generator import _file_name
from app.store.base import RunStore
from app.workflow import returns
from app.workflow.compile_stage import ARTIFACT as CONTRACT, WrongState, load_current_contract, save_contract_version
from app.workflow.feasibility_stage import ARTIFACT as FEASIBILITY
from app.workflow.harvest_stage import write_json
from app.workflow.model_stage import TMDL_ARTIFACT as TMDL
from app.workflow.plan_stage import MODEL_ARTIFACT as SPEC, MODEL_FILE, PLAN_ARTIFACT as PLAN
from app.workflow.report_plan_stage import ARTIFACT as REPORT_PLAN, CURRENT as REPORT_PLAN_FILE
from app.workflow.report_stage import ARTIFACT as PBIR
from app.workflow.returns import ReturnRecord
from app.workflow.run import Run, utc_now
from app.workflow.source_stage import ARTIFACT as SOURCES
from app.workflow.stale import STALE, stale_names
from app.workflow.states import RunState, can_transition

IMPACT_FILE = "Impact.json"
ALL = "*"  # the whole artifact is affected (and regenerated without a scope guard)
S = RunState

# How far along the pipeline each state is. A return is resolved when the run is back at least as far.
RANK = {
    S.CREATED: 0, S.NEEDS_CLARIFICATION: 0, S.REQUIREMENT_APPROVED: 1, S.DATA_PROFILED: 2, S.PLAN_APPROVED: 3,
    S.MODEL_GENERATING: 4, S.MODEL_FAILED: 4, S.MODEL_APPROVED: 5, S.REPORT_GENERATING: 6, S.REPORT_FAILED: 6,
    S.REPORT_APPROVED: 7, S.DEPLOYING: 8, S.ROLLED_BACK: 8, S.COMPLETED: 9,
}
TARGET_STATE = {"REQUIREMENT_ANALYSIS": S.CREATED, "PLANNING": S.DATA_PROFILED, "MODEL": S.PLAN_APPROVED}
RETURN_FROM = {
    "REQUIREMENT_ANALYSIS": (S.REQUIREMENT_APPROVED, S.DATA_PROFILED, S.PLAN_APPROVED, S.MODEL_APPROVED, S.REPORT_APPROVED, S.NEEDS_INPUT),
    "PLANNING": (S.PLAN_APPROVED, S.MODEL_APPROVED, S.REPORT_APPROVED),
    "MODEL": (S.MODEL_APPROVED, S.REPORT_APPROVED),
}
KPI_FIELDS = ("formula_intent", "grain", "filters", "time_logic", "exclusions", "currency", "expected_examples")
GLOBAL_FIELDS = ("grain", "filters", "currency", "exclusions")  # a change applies to every KPI


# --- the response shapes -------------------------------------------------------------------------


class ChangedArtifact(BaseModel):
    artifact: str
    objects: list[str]


class StaleArtifact(BaseModel):
    artifact: str
    objects: list[str]
    reason: str
    removals: list[str] = Field(default_factory=list)  # objects marked for removal: shown for review, never dropped silently


class OpenReturn(BaseModel):
    return_id: str
    to: str | None
    from_state: str | None
    reason: str
    at: str


class ImpactReport(BaseModel):
    changed: list[ChangedArtifact] = Field(default_factory=list)
    stale: list[StaleArtifact] = Field(default_factory=list)
    open_return: OpenReturn | None = None


# --- returning a run -----------------------------------------------------------------------------


def snapshot(run: Run) -> dict[str, dict[str, Any]]:
    return {n: {"version": r.version, "hash": r.hash, "path": r.path, "status": r.status} for n, r in run.artifacts.items()}


def return_run(
    store: RunStore, runs_root: Path, run_id: str, to: str, reason: str, user: dict[str, str]
) -> Run:
    """`POST /runs/{id}/return`. Raises WrongState (e.g. DEPLOYING) before anything changes."""
    if to not in TARGET_STATE:
        raise ValueError(f"to must be one of {', '.join(TARGET_STATE)}")
    run = store.get(run_id)
    allowed = RETURN_FROM[to]
    ok = run.state in allowed
    if run.state is S.NEEDS_INPUT:  # only when it was waiting after real progress (a rejected model, a gap)
        ok = run.resume_state is not None and RANK.get(run.resume_state, 0) >= RANK[S.REQUIREMENT_APPROVED] and to == "REQUIREMENT_ANALYSIS"
    if not ok:
        raise WrongState(f"Returning to {to}", run.state, allowed)
    target = TARGET_STATE[to]
    if not can_transition(run.state, target, run.resume_state):
        raise WrongState(f"Returning to {to}", run.state, allowed)

    actor = user["upn"]
    opened = returns.open_return(runs_root, run_id)
    baseline = opened.baseline if opened else snapshot(run)  # a second return keeps the original baseline
    from_state = opened.from_state if opened else (run.resume_state.value if run.state is S.NEEDS_INPUT and run.resume_state else run.state.value)

    unlocked: list[str] = []
    if to == "REQUIREMENT_ANALYSIS":
        contract = load_current_contract(runs_root, run_id)
        if contract is not None and contract.status is bc.ContractStatus.APPROVED:
            draft = contract.model_copy(update={"status": bc.ContractStatus.DRAFT, "version": contract.version + 1})
            save_contract_version(store, runs_root, run, draft, status="draft")  # a new version; the approved one stays
            unlocked.append(CONTRACT)
    elif to == "PLANNING":
        for name in (PLAN, SPEC):
            if name in run.artifacts and run.artifacts[name].status == "approved":
                run.artifacts[name].status, run.artifacts[name].updated_at = "draft", utc_now()
                unlocked.append(name)
        store.save(run)
    else:
        if TMDL in run.artifacts and run.artifacts[TMDL].status == "approved":
            run.artifacts[TMDL].status, run.artifacts[TMDL].updated_at = "draft", utc_now()
            unlocked.append(TMDL)
        store.save(run)

    run = store.transition(run_id, target, f"Returned to {to.lower().replace('_', ' ')}: {reason}", actor)
    returns.append(
        runs_root,
        ReturnRecord(
            return_id=returns.new_return_id(), run_id=run_id, at=utc_now(), user={"oid": user.get("oid", ""), "upn": actor},
            to=to, from_state=from_state, new_state=run.state.value, reason=reason, baseline=baseline, unlocked=unlocked,
            approvals_before=returns.count_approvals(runs_root, run_id),
        ),
    )
    from app.workflow import audit  # avoids a circular import

    audit.record(store, runs_root, run_id)
    return run


# --- the object-level diff ------------------------------------------------------------------------


@dataclass
class Delta:
    """What changed upstream, as sets that the dependency map is applied to."""

    changed: dict[str, list[str]] = field(default_factory=dict)  # artifact -> objects, for the report
    kpis: dict[str, str] = field(default_factory=dict)  # casefolded name -> name, changed or added or removed
    kpis_removed: set[str] = field(default_factory=set)
    entities: list[str] = field(default_factory=list)
    dimensions: bool = False
    security: bool = False
    everything: bool = False  # something no object-level mapping covers: every dependent is regenerated whole
    measures: dict[str, set[str]] = field(default_factory=dict)  # ModelSpec measure -> its tables (changed, added or removed)
    measures_removed: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    tables_added_or_removed: bool = False
    relationships: list[tuple[str, str]] = field(default_factory=list)  # (from table, to table)
    roles: bool = False
    storage: bool = False


def _kpi_data(kpi: bc.Kpi) -> dict:
    return kpi.model_dump(mode="json", include=set(KPI_FIELDS))


def contract_delta(old: bc.BusinessContract, new: bc.BusinessContract, delta: Delta) -> None:
    """KPIs by name (a definition field changed, added or removed), entities, dimensions, security and the
    contract-level fields that apply to every KPI. The objective and other wording change nothing."""
    objects: list[str] = []
    old_kpis = {k.name.casefold(): k for k in old.kpis}
    new_kpis = {k.name.casefold(): k for k in new.kpis}
    for key in sorted(old_kpis.keys() | new_kpis.keys()):
        a, b = old_kpis.get(key), new_kpis.get(key)
        if a is None or b is None or _kpi_data(a) != _kpi_data(b):
            name = (b or a).name
            delta.kpis[key] = name
            if b is None:
                delta.kpis_removed.add(key)
            objects.append(f"kpi:{name}")
    if any(getattr(old, f) != getattr(new, f) for f in GLOBAL_FIELDS):
        for key, kpi in {**old_kpis, **new_kpis}.items():
            if key not in delta.kpis:
                delta.kpis[key] = kpi.name
                objects.append(f"kpi:{kpi.name}")
        objects.append("contract:" + ",".join(f for f in GLOBAL_FIELDS if getattr(old, f) != getattr(new, f)))
    old_e, new_e = {e.casefold(): e for e in old.entities}, {e.casefold(): e for e in new.entities}
    delta.entities = [new_e.get(k) or old_e[k] for k in sorted(old_e.keys() ^ new_e.keys())]
    objects += [f"entity:{e}" for e in delta.entities]
    if {d.casefold() for d in old.dimensions} != {d.casefold() for d in new.dimensions}:
        delta.dimensions = True
        objects.append("dimensions")
    if [s.model_dump() for s in old.security] != [s.model_dump() for s in new.security]:
        delta.security = True
        objects.append("security")
    if delta.entities:
        delta.everything = True  # a different entity can mean different tables everywhere
    if objects:
        delta.changed[CONTRACT] = objects


def spec_delta(old: ModelSpec, new: ModelSpec, delta: Delta) -> None:
    """Measures, tables, relationships and roles by name; the storage mode."""
    objects: list[str] = []

    def by_name(items) -> dict:
        return {i.name: i for i in items}

    old_m, new_m = by_name(old.measures), by_name(new.measures)
    for name in sorted(old_m.keys() | new_m.keys()):
        a, b = old_m.get(name), new_m.get(name)
        if a is None or b is None or a.model_dump() != b.model_dump():
            delta.measures.setdefault(name, set()).update(m.table for m in (a, b) if m is not None)
            if b is None:
                delta.measures_removed.add(name)
            objects.append(f"measure:{name}")
    old_t, new_t = by_name(old.tables), by_name(new.tables)
    for name in sorted(old_t.keys() | new_t.keys()):
        a, b = old_t.get(name), new_t.get(name)
        if a is None or b is None or a.model_dump(by_alias=True) != b.model_dump(by_alias=True):
            delta.tables.add(name)
            delta.tables_added_or_removed |= a is None or b is None
            objects.append(f"table:{name}")
    old_r, new_r = by_name(old.relationships), by_name(new.relationships)
    for name in sorted(old_r.keys() | new_r.keys()):
        a, b = old_r.get(name), new_r.get(name)
        if a is None or b is None or a.model_dump(by_alias=True) != b.model_dump(by_alias=True):
            r = b or a
            delta.relationships.append((r.from_.table, r.to.table))
            objects.append(f"relationship:{name}")
    if [r.model_dump() for r in old.roles] != [r.model_dump() for r in new.roles]:
        delta.roles = True
        objects.append("roles")
    if old.storage_mode != new.storage_mode:
        delta.storage = True
        objects.append("storage_mode")
    if objects:
        delta.changed[SPEC] = objects


# --- reading the baseline and the current files ----------------------------------------------------


def _load_contract_version(runs_root: Path, run_id: str, version: int) -> bc.BusinessContract | None:
    try:
        return bc.load(Path(runs_root) / run_id / "contracts" / f"BusinessContract.v{version}.yaml")
    except (bc.ContractInvalid, OSError, ValueError):
        return None


def _load_spec(path: Path) -> ModelSpec | None:
    try:
        return load_yaml(ModelSpec, path)
    except (OSError, ValueError, ValidationError):
        return None


def _load_report_plan(runs_root: Path, run_id: str) -> ReportPlan | None:
    try:
        return ReportPlan.model_validate_json((Path(runs_root) / run_id / REPORT_PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def compute_delta(store_run: Run, runs_root: Path, ret: ReturnRecord) -> Delta:
    """The upstream changes since the baseline. A contract counts once it is approved again, a ModelSpec
    once its plan is. The changed KPIs are mapped to their measures through the baseline and the
    current ModelSpec."""
    run_dir = Path(runs_root) / store_run.run_id
    delta = Delta()
    arts = store_run.artifacts

    base_spec: ModelSpec | None = None
    if SPEC in ret.baseline:
        base_spec = _load_spec(run_dir / "plans" / f"ModelSpec.v{ret.baseline[SPEC]['version']}.yaml")
    cur_spec = _load_spec(run_dir / MODEL_FILE) if SPEC in arts else None

    contract_ref = arts.get(CONTRACT)
    if contract_ref and contract_ref.status == "approved" and CONTRACT in ret.baseline and contract_ref.hash != ret.baseline[CONTRACT]["hash"]:
        old = _load_contract_version(runs_root, store_run.run_id, ret.baseline[CONTRACT]["version"])
        new = load_current_contract(runs_root, store_run.run_id)
        if old is None or new is None:
            delta.everything = True
            delta.changed[CONTRACT] = [ALL]
        else:
            contract_delta(old, new, delta)

    spec_ref = arts.get(SPEC)
    if spec_ref and spec_ref.status == "approved" and SPEC in ret.baseline and spec_ref.hash != ret.baseline[SPEC]["hash"]:
        if base_spec is None or cur_spec is None:
            delta.everything = True
            delta.changed[SPEC] = [ALL]
        else:
            spec_delta(base_spec, cur_spec, delta)
    _measures_of_kpis(delta, base_spec, cur_spec)
    return delta


def _measures_of_kpis(delta: Delta, *specs: ModelSpec | None) -> None:
    """The measures built from the changed KPIs (by `kpi` name), looked up in both ModelSpecs, so a
    measure that the new plan dropped or moved is still found."""
    for spec in specs:
        for m in (spec.measures if spec else []):
            if m.kpi.casefold() in delta.kpis:
                delta.measures.setdefault(m.name, set()).add(m.table)
                if m.kpi.casefold() in delta.kpis_removed:
                    delta.measures_removed.add(m.name)
    # a measure whose DAX uses a changed measure changes too, and so on up the chain
    grew = True
    while grew:
        grew = False
        for spec in specs:
            for m in (spec.measures if spec else []):
                if m.name in delta.measures or not m.dax:
                    continue
                used = {n.casefold() for n in extract_references(m.dax)[1]}
                if used & {n.casefold() for n in delta.measures}:
                    delta.measures[m.name] = {m.table}
                    grew = True


# --- applying the dependency map -------------------------------------------------------------------


def _visual_refs(visual: Visual) -> list[FieldRef]:
    refs = [*visual.fields.category, *visual.fields.series, *visual.fields.values]
    refs += [FieldRef(table=f.table, column=f.column) for f in visual.filters]
    if visual.drill:
        refs.append(FieldRef(table=visual.drill.table, hierarchy=visual.drill.hierarchy))
    return refs


def _visuals(plan: ReportPlan, measures: set[str], tables: set[str], removed: set[str]) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """The visuals that bind a changed measure or a column of a changed table, and those that bind a
    removed measure. Each is (page index, page name, visual id)."""
    hit, gone = [], []
    for i, page in enumerate(plan.pages):
        for v in page.visuals:
            refs = _visual_refs(v)
            used = {r.measure for r in refs if r.measure}
            if used & measures or any(r.table in tables for r in refs if not r.measure and r.table):
                hit.append((i, page.name, v.id))
            if used & removed:
                gone.append((i, page.name, v.id))
    return hit, gone


def _table_files(delta: Delta) -> tuple[list[str], bool]:
    """The TMDL files the changed objects live in, and whether everything must be regenerated."""
    if delta.everything or delta.storage:
        return [], True
    files: set[str] = set()
    for tables in delta.measures.values():
        files |= {f"definition/tables/{_file_name(t)}" for t in tables}
    for table in delta.tables:
        files.add(f"definition/tables/{_file_name(table)}")
    if delta.relationships:
        files.add("definition/relationships.tmdl")
        for a, b in delta.relationships:
            files |= {f"definition/tables/{_file_name(a)}", f"definition/tables/{_file_name(b)}"}
    if delta.tables_added_or_removed:
        files.add("definition/model.tmdl")
    if delta.roles or delta.security:
        files.add("definition/roles/")
    return sorted(files), False


def derive_stale(run: Run, runs_root: Path, delta: Delta) -> list[StaleArtifact]:
    """The stale artifacts and the objects in them, from the upstream delta."""
    arts = run.artifacts
    measures = set(delta.measures)
    removed = set(delta.measures_removed)
    out: list[StaleArtifact] = []
    kpi_objs = [f"kpi:{n}" for n in sorted(delta.kpis.values(), key=str.casefold)]
    contract_changed = CONTRACT in delta.changed
    entity_objs = [f"entity:{e}" for e in delta.entities]

    def add(name: str, objects: list[str], reason: str, removals: list[str] | None = None) -> None:
        if name in arts and (objects or removals):
            out.append(StaleArtifact(artifact=name, objects=objects, reason=reason, removals=removals or []))

    if contract_changed:
        what = "the business contract changed"
        plan_objs = [*kpi_objs, *(["roles"] if delta.security else [])]
        for name, objs in ((SOURCES, [*kpi_objs, *entity_objs]), (FEASIBILITY, kpi_objs), (PLAN, plan_objs)):
            add(name, [ALL] if delta.everything else objs, f"{what}: {', '.join(delta.changed[CONTRACT])}")
        spec_objs = [f"measure:{n}" for n in sorted(measures)] + (["roles"] if delta.security else [])
        add(SPEC, [ALL] if delta.everything else spec_objs, f"{what}: {', '.join(delta.changed[CONTRACT])}",
            [f"measure:{n}" for n in sorted(removed)])

    if measures or delta.tables or delta.relationships or delta.roles or delta.security or delta.storage or delta.everything:
        files, whole = _table_files(delta)
        objs = [ALL] if whole else [*[f"measure:{n}" for n in sorted(measures)], *[f"table:{t}" for t in sorted(delta.tables)], *[f"file:{f}" for f in files]]
        reason = "the model specification changed" if SPEC in delta.changed else "its measures come from a changed KPI"
        add(TMDL, objs, reason, [f"measure:{n}" for n in sorted(removed)])

        plan = _load_report_plan(runs_root, run.run_id)
        if plan is None and (REPORT_PLAN in arts or PBIR in arts):
            add(REPORT_PLAN, [ALL], "the report plan can't be read, so all of it is checked again")
            add(PBIR, [ALL], "the report plan can't be read, so all of it is checked again")
        elif plan is not None:
            tables = set(delta.tables) | {t for pair in delta.relationships for t in pair}
            hit, gone = _visuals(plan, measures, tables, removed)
            if delta.everything or delta.storage:
                add(REPORT_PLAN, [ALL], "the model changed everywhere")
                add(PBIR, [ALL], "the model changed everywhere")
            else:
                add(REPORT_PLAN, [f"visual:{p}/{v}" for _, p, v in hit], "its visuals use a changed measure or table",
                    [f"visual:{p}/{v}" for _, p, v in gone])
                add(PBIR, [f"visual:p{i + 1}/{v}" for i, _, v in hit], "its visuals use a changed measure or table",
                    [f"visual:p{i + 1}/{v}" for i, _, v in gone])
    if delta.dimensions and contract_changed:
        add(REPORT_PLAN, [ALL], "the contract's dimensions changed, so its questions change")
        add(PBIR, [ALL], "the contract's dimensions changed, so its questions change")
    merged: dict[str, StaleArtifact] = {}
    for e in out:  # one entry per artifact
        if e.artifact in merged:
            m = merged[e.artifact]
            m.objects = [ALL] if ALL in (m.objects + e.objects) else sorted(set(m.objects + e.objects))
            m.removals = sorted(set(m.removals + e.removals))
            m.reason = m.reason if e.reason in m.reason else f"{m.reason}; {e.reason}"
        else:
            merged[e.artifact] = e
    return list(merged.values())


# --- persistence and marking ---------------------------------------------------------------------------


def _state_file(runs_root: Path, run_id: str) -> Path:
    return Path(runs_root) / run_id / IMPACT_FILE


def _read_state(runs_root: Path, run_id: str, return_id: str) -> dict:
    try:
        data = json.loads(_state_file(runs_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if data.get("return_id") == return_id else {}


def _is_rebuilt(run: Run, ret: ReturnRecord, name: str, fresh: dict) -> bool:
    ref = run.artifacts[name]
    base = ret.baseline.get(name)
    return (base is not None and ref.hash != base["hash"]) or fresh.get(name) == ref.hash


def compute(run: Run, runs_root: Path, ret: ReturnRecord) -> tuple[list[ChangedArtifact], list[StaleArtifact], dict]:
    state = _read_state(runs_root, run.run_id, ret.return_id)
    fresh = dict(state.get("fresh", {}))
    delta = compute_delta(run, runs_root, ret)
    stale = [e for e in derive_stale(run, runs_root, delta) if not _is_rebuilt(run, ret, e.artifact, fresh)]
    changed = [ChangedArtifact(artifact=a, objects=o) for a, o in delta.changed.items()]
    return changed, stale, state


def refresh_impact(store: RunStore, runs_root: Path, run_id: str) -> ImpactReport | None:
    """Recompute the impact of the open return, mark the stale artifacts and save `Impact.json`.
    Idempotent. None if the run has no open return."""
    ret = returns.open_return(runs_root, run_id)
    if ret is None:
        return None
    run = store.get(run_id)
    changed, stale, state = compute(run, runs_root, ret)
    prior = dict(state.get("prior_status", {}))
    names = {e.artifact for e in stale}
    dirty = False
    for name, ref in run.artifacts.items():
        if ref.status == STALE and name not in names:  # no longer affected
            ref.status, ref.updated_at, dirty = prior.pop(name, "draft"), utc_now(), True
    for name in sorted(names):
        ref = run.artifacts[name]
        if ref.status != STALE:
            prior[name] = ref.status
            ref.status, ref.updated_at, dirty = STALE, utc_now(), True
    if dirty:
        store.save(run)
    report = ImpactReport(changed=changed, stale=stale, open_return=_open(ret))
    write_json(
        _state_file(runs_root, run_id),
        {
            "schema_version": 1, "run_id": run_id, "return_id": ret.return_id, "computed_at": utc_now(),
            "changed": [c.model_dump() for c in changed], "stale": [e.model_dump() for e in stale],
            "prior_status": {n: s for n, s in prior.items() if n in names}, "fresh": state.get("fresh", {}),
        },
    )
    return report


def _open(ret: ReturnRecord) -> OpenReturn:
    return OpenReturn(return_id=ret.return_id, to=ret.to, from_state=ret.from_state, reason=ret.reason, at=ret.at)


def read_impact(store: RunStore, runs_root: Path, run_id: str) -> ImpactReport:
    """`GET /runs/{id}/impact`. Without an open return there is nothing to compare with."""
    store.get(run_id)
    return refresh_impact(store, runs_root, run_id) or ImpactReport()


def mark_fresh(store: RunStore, runs_root: Path, run_id: str, names: list[str]) -> None:
    """The artifacts were regenerated or checked again and came out as they were: they are no longer
    stale. Their earlier status is restored and their current hash is remembered."""
    ret = returns.open_return(runs_root, run_id)
    if ret is None:
        return
    run = store.get(run_id)
    state = _read_state(runs_root, run_id, ret.return_id)
    fresh = dict(state.get("fresh", {}))
    prior = state.get("prior_status", {})
    for name in names:
        ref = run.artifacts.get(name)
        if ref is None:
            continue
        if ref.status == STALE:
            ref.status, ref.updated_at = prior.get(name, "draft"), utc_now()
        fresh[name] = ref.hash
    store.save(run)
    state["fresh"] = fresh
    write_json(_state_file(runs_root, run_id), {**state, "return_id": ret.return_id, "run_id": run_id, "schema_version": 1})
    refresh_impact(store, runs_root, run_id)


def close_if_done(store: RunStore, runs_root: Path, run_id: str, note: str = "Approved again after the return") -> bool:
    """Resolve the open return once nothing is stale and the run is as far along as it was."""
    ret = returns.open_return(runs_root, run_id)
    if ret is None:
        return False
    run = store.get(run_id)
    if stale_names(run.artifacts):
        return False
    state = run.resume_state if run.state is S.NEEDS_INPUT and run.resume_state else run.state
    if RANK.get(state, 0) < RANK.get(S(ret.from_state), 0) or run.state is S.NEEDS_INPUT:
        return False
    returns.append(runs_root, ReturnRecord(event="resolved", return_id=ret.return_id, run_id=run_id, at=utc_now(), reason=note))
    return True


def on_accept(store: RunStore, runs_root: Path, run_id: str) -> None:
    """After any Accept: mark what the newly approved artifact made stale, and close the return if done."""
    if returns.open_return(runs_root, run_id) is None:
        return
    refresh_impact(store, runs_root, run_id)
    close_if_done(store, runs_root, run_id)
