"""Deterministic checks of a ReportPlan (T-601, FR-013): RPLAN.1-RPLAN.7.

The model's proposal is a candidate; these checks decide whether it stands. Every failure names
its path (a visual id, a page, a field) so a reviewer or a retry can fix exactly that.
"""

from pydantic import ValidationError

from app.contracts.model_plan import ModelSpec
from app.contracts.report_plan import FIELD_ROLES, INTERACTION_TYPES, VISUAL_TYPES, FieldRef, Filter, ReportPlan, to_data
from app.workflow.gates import GateCheck, GateResult
from app.workflow.harvest_stage import canonical_hash
from app.workflow.run import utc_now

MAX_VISUALS_PER_PAGE = 8
ARTIFACT = "ReportPlan"
GATE = "REPORT.PLAN"

DESCRIPTIONS = {
    "RPLAN.1": "The plan matches the ReportPlan schema",
    "RPLAN.2": "Every referenced field, measure and hierarchy exists in the approved ModelSpec",
    "RPLAN.3": "Every question maps to at least one visual or is listed as unanswered",
    "RPLAN.4": "Visual IDs and page names are unique",
    "RPLAN.5": "Every visual type is supported",
    "RPLAN.6": "No page has more than 8 visuals",
    "RPLAN.7": "Every interaction refers to existing visuals and a valid type",
}


def _check(id_: str, paths: list[str]) -> GateCheck:
    return GateCheck(
        id=id_,
        description=DESCRIPTIONS[id_],
        status="fail" if paths else "pass",
        evidence={"paths": paths} if paths else {},
    )


def _schema_paths(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def _field_errors(where: str, ref: FieldRef, spec: ModelSpec, role: str) -> list[str]:
    measures = {m.name: m for m in spec.measures}
    tables = {t.name: t for t in spec.tables}
    if ref.measure is not None:
        if ref.table or ref.column or ref.hierarchy:
            return [f"{where}: a measure reference must not also name a table, column or hierarchy"]
        measure = measures.get(ref.measure)
        if measure is None:
            return [f"{where}: measure '{ref.measure}' is not in the approved model"]
        if not measure.dax:
            return [f"{where}: measure '{ref.measure}' has no approved DAX"]
        return []
    if role == "values":
        return [f"{where}: values must be measures of the approved model, not columns or hierarchies"]
    table = tables.get(ref.table or "")
    if table is None:
        return [f"{where}: table '{ref.table}' is not in the approved model"]
    if (ref.column is None) == (ref.hierarchy is None):
        return [f"{where}: name exactly one of a measure, a column or a hierarchy"]
    if ref.column is not None:
        if ref.column not in {c.name for c in table.columns}:
            return [f"{where}: column '{ref.table}'[{ref.column}] is not in the approved model"]
    elif ref.hierarchy not in {h.name for h in table.hierarchies}:
        return [f"{where}: hierarchy '{ref.table}'.{ref.hierarchy} is not in the approved model"]
    return []


def _filter_errors(where: str, flt: Filter, spec: ModelSpec) -> list[str]:
    table = next((t for t in spec.tables if t.name == flt.table), None)
    if table is None:
        return [f"{where}: table '{flt.table}' is not in the approved model"]
    if flt.column not in {c.name for c in table.columns}:
        return [f"{where}: column '{flt.table}'[{flt.column}] is not in the approved model"]
    return []


def validate_report_plan(data: dict | ReportPlan, spec: ModelSpec) -> tuple[ReportPlan | None, GateResult]:
    """Check a plan (parsed or raw data). Returns the parsed plan (None if it fails the schema) and the result."""
    plan = data if isinstance(data, ReportPlan) else None
    schema_paths: list[str] = []
    if plan is None:
        try:
            plan = ReportPlan.model_validate(data)
        except ValidationError as exc:
            schema_paths = _schema_paths(exc)
    checks = [_check("RPLAN.1", schema_paths)]
    if plan is None:
        checks += [
            GateCheck(id=i, description=DESCRIPTIONS[i], status="fail", evidence={"paths": ["not checked: the plan fails the schema"]})
            for i in list(DESCRIPTIONS)[1:]
        ]
        return None, GateResult(gate=GATE, artifact=ARTIFACT, artifact_hash="", passed=False, checked_at=utc_now(), checks=checks)

    refs: list[str] = []
    types: list[str] = []
    sizes: list[str] = []
    interactions: list[str] = []
    coverage: list[str] = []
    seen: dict[str, int] = {}
    for page in plan.pages:
        if len(page.visuals) > MAX_VISUALS_PER_PAGE:
            sizes.append(f"pages[{page.name}]: {len(page.visuals)} visuals, at most {MAX_VISUALS_PER_PAGE} (split the page)")
        for i, flt in enumerate(page.filters):
            refs += _filter_errors(f"pages[{page.name}].filters[{i}]", flt, spec)
        for visual in page.visuals:
            seen[visual.id] = seen.get(visual.id, 0) + 1
            if visual.type not in VISUAL_TYPES:
                types.append(f"{visual.id}: type '{visual.type}' is not supported; allowed: {', '.join(VISUAL_TYPES)}")
            if not any(getattr(visual.fields, role) for role in FIELD_ROLES):
                refs.append(f"{visual.id}: the visual binds no field")
            for role in FIELD_ROLES:
                for n, ref in enumerate(getattr(visual.fields, role)):
                    refs += _field_errors(f"{visual.id}.fields.{role}[{n}]", ref, spec, role)
            for n, flt in enumerate(visual.filters):
                refs += _filter_errors(f"{visual.id}.filters[{n}]", flt, spec)
            if visual.drill is not None:
                owners = [t for t in spec.tables if visual.drill.table in (None, t.name)]
                if not any(h.name == visual.drill.hierarchy for t in owners for h in t.hierarchies):
                    refs.append(f"{visual.id}.drill: hierarchy '{visual.drill.hierarchy}' is not in the approved model")
    ids = [f"visual id '{vid}' is used {n} times" for vid, n in seen.items() if n > 1]
    page_names: dict[str, int] = {}
    for page in plan.pages:
        page_names[page.name] = page_names.get(page.name, 0) + 1
    ids += [f"page name '{name}' is used {n} times" for name, n in page_names.items() if n > 1]
    question_ids = {q.id for q in plan.questions}
    covered = {q for page in plan.pages for v in page.visuals for q in v.question_ids}
    for page in plan.pages:
        for visual in page.visuals:
            for q in visual.question_ids:
                if q not in question_ids:
                    coverage.append(f"{visual.id}: question '{q}' is not in the plan's questions")
    unanswered = {u.question_id for u in plan.unanswered_questions}
    for u in sorted(unanswered - question_ids):
        coverage.append(f"unanswered_questions: '{u}' is not in the plan's questions")
    for q in plan.questions:
        if q.id not in covered and q.id not in unanswered:
            coverage.append(f'{q.id}: no visual answers "{q.text}" and it is not listed as unanswered')
    for n, it in enumerate(plan.interactions):
        where = f"interactions[{n}]"
        for end in (it.from_visual, it.to_visual):
            if end not in seen:
                interactions.append(f"{where}: visual '{end}' does not exist")
        if it.type not in INTERACTION_TYPES:
            interactions.append(f"{where}: type '{it.type}' is not one of {', '.join(INTERACTION_TYPES)}")
    checks += [
        _check("RPLAN.2", refs),
        _check("RPLAN.3", coverage),
        _check("RPLAN.4", ids),
        _check("RPLAN.5", types),
        _check("RPLAN.6", sizes),
        _check("RPLAN.7", interactions),
    ]
    result = GateResult(
        gate=GATE,
        artifact=ARTIFACT,
        artifact_hash=canonical_hash(to_data(plan)),
        passed=all(c.status != "fail" for c in checks),
        checked_at=utc_now(),
        checks=checks,
    )
    return plan, result


def failures(result: GateResult) -> list[str]:
    return [p for c in result.checks if c.status == "fail" for p in c.evidence.get("paths", [c.description])]
