"""Targeted regeneration of stale artifacts (T-701, FR-020).

`regenerate_stale` rebuilds only the stale objects, one stage at a time, reusing the generators of
T-401/T-402 (sources, feasibility), T-404 (plan), T-502/T-503 (model), T-601/T-602 (report) and the
validators of T-504/T-603. A stage that needs a person's approval (G3 for the plan, G4 for the model, G5
for the report) is where a call stops: after the approval, the next call continues with the next
stage. Every object outside the impact set stays as it was: the plan layer keeps the saved objects, and
the model and report folders are compared with the version in place before a new one is kept
(`scope_guard`, as in T-505).

Where nothing is stale and the artifacts are still approved, a call walks the run back up to the stage it
was returned from through the legal transitions, because nothing that was approved has changed.

Objects that were removed (a deleted KPI's measure and visuals) are never dropped silently: they are
reported in `needs_review`.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml
from app.report.plan_validation import failures, validate_report_plan
from app.report.planner import derive_questions
from app.store.base import RunStore
from app.workflow import returns
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.feasibility_stage import check_feasibility
from app.workflow.gates import GateResult
from app.workflow.harvest_stage import ARTIFACT as INVENTORY, canonical_hash, write_json
from app.workflow.impact import (
    ALL, CONTRACT, FEASIBILITY, PBIR, PLAN, RANK, REPORT_PLAN, SOURCES, SPEC, TMDL, close_if_done, mark_fresh, refresh_impact,
)
from app.workflow.inventory_review import check_g2, raise_data_gaps
from app.workflow.measure_stage import add_measures
from app.workflow.model_stage import generate_model
from app.workflow.plan_stage import MODEL_FILE, generate_plan
from app.workflow.report_plan_stage import CURRENT as REPORT_PLAN_FILE, SEMANTIC_MODEL
from app.workflow.report_stage import generate_report
from app.workflow.report_validate_stage import validate_report
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.source_stage import resolve_sources
from app.workflow.states import RunState
from app.workflow.validate_stage import validate_model

S = RunState
ALLOWED_STATES = (S.REQUIREMENT_APPROVED, S.DATA_PROFILED, S.PLAN_APPROVED, S.MODEL_APPROVED)
MAX_STEPS = 8


@dataclass
class StaleServices:
    """What the generators need, built by the API from the signed-in user."""

    source_llm: Any
    feasibility_llm: Any
    dax_llm: Any
    recon_llm: Any
    items: Any
    pbi: Any
    sql_factory: Any
    user_upn: str | None = None


class Regenerated(BaseModel):
    artifact: str
    objects: list[str] = Field(default_factory=list)
    version: int | None = None
    note: str | None = None


class ValidationSummary(BaseModel):
    artifact: str
    gate: str
    passed: bool
    failed_checks: list[str] = Field(default_factory=list)


class RegenerateResult(BaseModel):
    regenerated: list[Regenerated] = Field(default_factory=list)
    validation: list[ValidationSummary] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)  # removed objects and anything a person must decide


@dataclass
class Ctx:
    store: RunStore
    runs_root: Path
    run_id: str
    services: StaleServices
    actor: str
    from_rank: int
    result: RegenerateResult = field(default_factory=RegenerateResult)
    stale: dict = field(default_factory=dict)  # artifact -> StaleArtifact, re-read before every step

    @property
    def run_dir(self) -> Path:
        return Path(self.runs_root) / self.run_id

    def run(self) -> Run:
        return self.store.get(self.run_id)

    def objects(self, name: str) -> list[str]:
        return self.stale[name].objects if name in self.stale else []

    def whole(self, *names: str) -> bool:
        return any(ALL in self.objects(n) for n in names)


def _summarize(artifact: str, gate: str, passed: bool, checks: list) -> ValidationSummary:
    return ValidationSummary(
        artifact=artifact, gate=gate, passed=passed,
        failed_checks=sorted({c.id for c in checks if c.status == "fail"}),
    )


def _hop(c: Ctx, to: RunState, reason: str) -> None:
    c.store.transition(c.run_id, to, reason, c.actor)


def _approved(run: Run, *names: str) -> bool:
    return all(n in run.artifacts and run.artifacts[n].status == "approved" for n in names)


# --- the plan layer ----------------------------------------------------------------------------------


def _merge_measures(kpis: set[str], notes: list[str]):
    """Keep every object of the saved ModelSpec and ExecutionPlan except the measures of `kpis`, which come
    from the new plan. If a new measure needs a table the saved model doesn't have, the new plan is used
    whole and the note says so."""

    def merge(old_model: ModelSpec, old_plan: ExecutionPlan, new_model: ModelSpec, new_plan: ExecutionPlan):
        saved = {m.name: m for m in old_model.measures}
        merged = old_model.model_copy(deep=True)
        merged.measures = [m if m.kpi.casefold() in kpis or m.name not in saved else saved[m.name] for m in new_model.measures]
        merged.based_on = dict(new_model.based_on)
        tables = {t.name for t in merged.tables}
        if any(m.table not in tables for m in merged.measures):
            notes.append("a changed KPI needs a table the approved model doesn't have, so the whole plan was generated again")
            return new_model, new_plan
        return merged, old_plan.model_copy(deep=True)

    return merge


def _plan_layer(c: Ctx) -> bool:
    names = [n for n in (SOURCES, FEASIBILITY, PLAN, SPEC) if n in c.stale]
    run = c.run()
    if not names:
        if c.from_rank >= RANK[S.PLAN_APPROVED] and _approved(run, PLAN, SPEC):
            _hop(c, S.PLAN_APPROVED, "Nothing in the plan is stale; the approved plan stands")
            return True
        return False
    whole = c.whole(*names)
    kpi_objs = {o for n in names for o in c.objects(n) if o.startswith("kpi:")}
    other = {o for n in names for o in c.objects(n) if not o.startswith("kpi:")}
    restrict = None if whole else {*kpi_objs, *{o for o in other if o.startswith("entity:")}}
    keep_structure = not whole and not other  # roles, entities and the like change more than measures
    kpis = {o[4:].casefold() for o in kpi_objs}

    if SOURCES in c.stale:
        resolution = resolve_sources(c.store, c.runs_root, c.run_id, c.services.source_llm, restrict=restrict)
        c.result.regenerated.append(Regenerated(artifact=SOURCES, objects=c.objects(SOURCES), version=resolution.version))
        mark_fresh(c.store, c.runs_root, c.run_id, [SOURCES])
    if FEASIBILITY in c.stale:
        report, run = check_feasibility(
            c.store, c.runs_root, c.run_id, c.services.feasibility_llm, actor=c.actor,
            restrict=None if whole else {o for o in kpi_objs},
        )
        c.result.regenerated.append(Regenerated(artifact=FEASIBILITY, objects=c.objects(FEASIBILITY), version=report.version))
        mark_fresh(c.store, c.runs_root, c.run_id, [FEASIBILITY])
        gaps = [k.kpi for k in report.kpis if k.status.value == "data_gap" and (whole or f"kpi:{k.kpi}".casefold() in {o.casefold() for o in kpi_objs})]
        if run.state is not S.DATA_PROFILED:
            c.result.needs_review.append(
                "KPI(s) " + ", ".join(gaps) + " can't be calculated from the available data after the change: "
                "a data gap was raised and the plan was not regenerated"
            )
            return False
    notes: list[str] = []
    plan, spec, gate = generate_plan(
        c.store, c.runs_root, c.run_id, merge=_merge_measures(kpis, notes) if keep_structure else None
    )
    refs = c.run().artifacts
    c.result.regenerated += [
        Regenerated(artifact=SPEC, objects=c.objects(SPEC), version=refs[SPEC].version, note="; ".join(notes) or None),
        Regenerated(artifact=PLAN, objects=c.objects(PLAN), version=refs[PLAN].version),
    ]
    c.result.validation.append(_summarize(PLAN, "PLAN", gate.passed, gate.checks))
    for name in (SPEC, PLAN):
        if c.stale.get(name) and c.stale[name].removals:
            c.result.needs_review += [f"{name}: {o} was removed because its KPI was deleted: review it in the plan" for o in c.stale[name].removals]
    mark_fresh(c.store, c.runs_root, c.run_id, [n for n in (SPEC, PLAN) if n in c.stale])
    return False  # the plan needs a bi_engineer's approval (G3) before the model


# --- the model layer -----------------------------------------------------------------------------------


def _allowed_files(objects: list[str]) -> set[str] | None:
    if ALL in objects:
        return None
    return {o[5:] for o in objects if o.startswith("file:")}


def _model_layer(c: Ctx) -> bool:
    run = c.run()
    tmdl, spec_ref = run.artifacts.get(TMDL), run.artifacts.get(SPEC)
    if tmdl is None or c.from_rank < RANK[S.MODEL_APPROVED]:
        return False
    needs = TMDL in c.stale or (spec_ref is not None and tmdl.based_on.get(SPEC) != spec_ref.hash)
    if not needs:
        if tmdl.status == "approved":
            _hop(c, S.MODEL_GENERATING, "Nothing in the model is stale; the approved model stands")
            _hop(c, S.MODEL_APPROVED, "Nothing in the model is stale; the approved model stands")
            return True
        return False
    objects = c.objects(TMDL)
    allowed = _allowed_files(objects) if TMDL in c.stale else None
    measures = None if ALL in objects or TMDL not in c.stale else {o[8:] for o in objects if o.startswith("measure:")}
    version, _, _ = generate_model(c.store, c.runs_root, c.run_id, actor=c.actor, expect_only=allowed)
    rejected: list[str] = []
    if measures is None or measures:
        results, version = add_measures(
            c.store, c.runs_root, c.run_id, c.services.dax_llm, only=measures, expect_only=allowed
        )
        rejected = [f"measure {r['name']}: {r.get('reason')}" for r in results if r["status"] == "rejected"]
    c.result.regenerated.append(Regenerated(artifact=TMDL, objects=objects, version=version))
    entry, run = validate_model(
        c.store, c.runs_root, c.run_id, items_client=c.services.items, pbi=c.services.pbi,
        sql_connector_factory=c.services.sql_factory, recon_llm=c.services.recon_llm,
        user_upn=c.services.user_upn, actor=c.actor,
    )
    c.result.validation.append(_summarize("SemanticModel", "G4", entry.passed, entry.checks))
    c.result.needs_review += [f"{o} was removed from the model because its KPI was deleted" for o in (c.stale[TMDL].removals if TMDL in c.stale else [])]
    mark_fresh(c.store, c.runs_root, c.run_id, [TMDL])
    if rejected and run.state is S.MODEL_GENERATING:
        raise_data_gaps(
            c.store, c.runs_root, c.run_id,
            [f"{r}. No DAX was invented; supply the definition or drop the KPI." for r in rejected], c.actor, states=(S.MODEL_GENERATING,),
        )
        c.result.needs_review += rejected
    return False  # the model needs the role approvals (G4) before the report


# --- the report layer ----------------------------------------------------------------------------------


def _restamp_report_plan(c: Ctx, run: Run) -> bool:
    """Check the saved ReportPlan against the new ModelSpec and contract and point it at the new model.
    False (with the reason in `needs_review`) if it no longer holds; nothing is changed then."""
    path = c.run_dir / REPORT_PLAN_FILE
    spec = load_yaml(ModelSpec, c.run_dir / MODEL_FILE)
    plan, result = validate_report_plan(json.loads(path.read_text(encoding="utf-8")), spec)
    if plan is None or not result.passed:
        c.result.needs_review.append(
            "The report plan no longer fits the model: " + "; ".join(failures(result)[:5])
            + ". Regenerate those visuals (approval action REGENERATE) or plan the report again (POST /report/plan)."
        )
        return False
    contract = load_current_contract(c.runs_root, c.run_id)
    questions, _ = derive_questions(contract, spec) if contract else ([], {})
    if [q.model_dump() for q in questions] != [q.model_dump() for q in plan.questions] or plan.unanswered_questions:
        c.result.needs_review.append(
            "The contract's questions changed, so the report plan has to be made again (POST /report/plan)."
        )
        return False
    based_on = {SEMANTIC_MODEL: run.artifacts[TMDL].hash, CONTRACT: run.artifacts[CONTRACT].hash}
    if plan.based_on != based_on:
        plan = plan.model_copy(update={"based_on": based_on, "version": plan.version + 1})
        data = plan.to_dict()
        write_json(c.run_dir / "reports" / f"ReportPlan.v{plan.version}.json", data)
        write_json(path, data)
        run.artifacts[REPORT_PLAN] = ArtifactRef(
            path=REPORT_PLAN_FILE, version=plan.version, hash=canonical_hash(data), status="draft", based_on=based_on, updated_at=utc_now()
        )
        c.store.save(run)
    c.result.regenerated.append(Regenerated(artifact=REPORT_PLAN, objects=c.objects(REPORT_PLAN), version=plan.version, note="checked against the new model and re-pointed at it"))
    mark_fresh(c.store, c.runs_root, c.run_id, [REPORT_PLAN])
    return True


def _report_layer(c: Ctx) -> bool:
    run = c.run()
    plan_ref, pbir, tmdl = run.artifacts.get(REPORT_PLAN), run.artifacts.get(PBIR), run.artifacts.get(TMDL)
    if plan_ref is None or pbir is None or tmdl is None or c.from_rank < RANK[S.REPORT_APPROVED]:
        return False
    needs = (
        REPORT_PLAN in c.stale or PBIR in c.stale
        or plan_ref.based_on.get(SEMANTIC_MODEL) != tmdl.hash or pbir.based_on.get(SEMANTIC_MODEL) != tmdl.hash
    )
    if not needs:
        if _approved(run, PBIR, REPORT_PLAN):
            _hop(c, S.REPORT_GENERATING, "Nothing in the report is stale; the approved report stands")
            _hop(c, S.REPORT_APPROVED, "Nothing in the report is stale; the approved report stands")
            return True
        return False
    if not _restamp_report_plan(c, run):
        return False
    objects = c.objects(PBIR)
    allowed: set[str] | None = None
    if ALL not in objects:  # the visuals that use a changed object, and the binding to the model
        allowed = {"definition.pbir"}
        for o in objects:
            page, _, visual = o[len("visual:"):].partition("/")
            allowed.add(f"definition/pages/{page}/visuals/{visual}/visual.json")
    version, _, published, run = generate_report(c.store, c.runs_root, c.run_id, c.services.items, c.actor, expect_only=allowed)
    c.result.regenerated.append(Regenerated(artifact=PBIR, objects=objects, version=version, note=f"{published['operation']} in the sandbox workspace"))
    entry, run = validate_report(c.store, c.runs_root, c.run_id, items_client=c.services.items, pbi=c.services.pbi, actor=c.actor)
    c.result.validation.append(_summarize("Report", "G5", entry.passed, entry.checks))
    for name in (REPORT_PLAN, PBIR):
        if name in c.stale:
            c.result.needs_review += [f"{name}: {o} was removed because its KPI was deleted: review it in the report" for o in c.stale[name].removals]
    mark_fresh(c.store, c.runs_root, c.run_id, [REPORT_PLAN, PBIR])
    return False  # the report needs both approvals (G5)


# --- the requirement layer ---------------------------------------------------------------------------


def _requirement_layer(c: Ctx) -> bool:
    if c.from_rank < RANK[S.DATA_PROFILED]:
        return False
    run = c.run()
    if not _approved(run, INVENTORY):
        c.result.needs_review.append("The metadata inventory isn't approved, so the run can't go back to the data stage: accept it again (G2)")
        return False
    gate: GateResult = check_g2(c.store, c.runs_root, c.run_id)
    c.result.validation.append(_summarize("MetadataInventory", "G2", gate.passed, gate.checks))
    if not gate.passed:
        c.result.needs_review.append(
            "Gate G2 no longer passes for the changed contract (" + ", ".join(sorted({x.id for x in gate.checks if x.status == 'fail'}))
            + "): review the inventory and accept it again"
        )
        return False
    _hop(c, S.DATA_PROFILED, "Requirement approved again; the inventory is unchanged and still passes G2")
    return True


STEPS = {
    S.REQUIREMENT_APPROVED: _requirement_layer,
    S.DATA_PROFILED: _plan_layer,
    S.PLAN_APPROVED: _model_layer,
    S.MODEL_APPROVED: _report_layer,
}


def regenerate_stale(
    store: RunStore, runs_root: Path, run_id: str, services: StaleServices, actor: str = "system"
) -> tuple[RegenerateResult, Run]:
    """`POST /runs/{id}/regenerate-stale`. Raises WrongState outside the approved stages; with no open return
    or nothing stale it changes nothing but, where everything is still approved, the stage the run is in."""
    run = store.get(run_id)
    if run.state not in ALLOWED_STATES:
        raise WrongState("Regenerating stale artifacts", run.state, ALLOWED_STATES)
    ret = returns.open_return(runs_root, run_id)
    if ret is None:
        return RegenerateResult(), run
    ctx = Ctx(store, runs_root, run_id, services, actor, RANK[S(ret.from_state)])
    for _ in range(MAX_STEPS):
        report = refresh_impact(store, runs_root, run_id)
        ctx.stale = {e.artifact: e for e in (report.stale if report else [])}
        step = STEPS.get(store.get(run_id).state)
        if step is None or not step(ctx):
            break
    refresh_impact(store, runs_root, run_id)
    close_if_done(store, runs_root, run_id)
    from app.workflow import audit  # avoids a circular import

    audit.record(store, runs_root, run_id)
    return ctx.result, store.get(run_id)
