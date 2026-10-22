"""The report planner (T-601, FR-013): business questions from the approved contract, then a
model-proposed layout (pages, visuals, filters, drill paths, interactions) that code validates.

The model sees the contract's objective, KPIs and dimensions, the question list, and the ModelSpec's
table, column, hierarchy and measure names, types and descriptions. It never sees data values. A
question that no approved measure answers is flagged as unanswered by code; the model can't add a
measure, and a visual that binds anything outside the ModelSpec fails validation.
"""

import json
from typing import Callable

from app.contracts.business_contract import BusinessContract
from app.contracts.model_plan import ModelSpec
from app.contracts.report_plan import INTERACTION_TYPES, VISUAL_TYPES, Interaction, Question, ReportPlan, UnansweredQuestion
from app.llm.errors import LlmInvalidOutput
from app.report.plan_validation import MAX_VISUALS_PER_PAGE, failures, validate_report_plan

SCHEMA_NAME = "report_plan"
MAX_ATTEMPTS = 2
LlmCall = Callable[[str, str, dict], str]

SYSTEM_PROMPT = f"""You are Nexora's report planner for Power BI reports on Microsoft Fabric.
You get the approved business contract, a list of business questions and the approved semantic model's
objects. Design the pages and visuals that answer the questions.

Rules:
1. Use only the tables, columns, hierarchies and measures listed in the model. Never invent a measure
   or a column. Values of a visual must be measures; categories and series are columns or hierarchies.
2. Refer to a measure as {{"measure": "<name>"}}, a column as {{"table": "<table>", "column": "<column>"}}
   and a hierarchy as {{"table": "<table>", "hierarchy": "<name>"}}; set every unused key to null.
3. Visual types: {", ".join(VISUAL_TYPES)}. For a pivotTable, category are the rows and series the
   columns. A slicer has only category fields. A card has only values.
4. Every question must be answered by at least one visual (list its id in question_ids). If no listed
   measure can answer a question, list it in unanswered_questions with the reason; never answer it
   with a different measure.
5. At most {MAX_VISUALS_PER_PAGE} visuals per page: split larger reports across pages. Give each visual a local id (any
   short unique text) used by interactions.
6. Interaction types: {", ".join(INTERACTION_TYPES)}. Use drill only with a hierarchy of the model.
7. Do not add a filter value the contract doesn't state. You see names and types only, never data.
Return JSON only."""


# --- the questions ------------------------------------------------------------------------------


def derive_questions(contract: BusinessContract, spec: ModelSpec) -> tuple[list[Question], dict[str, str]]:
    """The contract's questions, and the ones no approved measure can answer (id -> reason)."""
    measured = {m.kpi.casefold() for m in spec.measures if m.dax}
    questions: list[Question] = []
    forced: dict[str, str] = {}
    for kpi in contract.kpis:
        qid = f"Q-R{len(questions) + 1}"
        text = f"How does {kpi.name} trend over time ({kpi.time_logic})?" if kpi.time_logic else f"What is {kpi.name}?"
        questions.append(Question(id=qid, text=text, source=f"contract.kpis[{kpi.name}]"))
        if kpi.name.casefold() not in measured:
            forced[qid] = f"The approved model has no measure for KPI '{kpi.name}'"
    for dimension in contract.dimensions:
        qid = f"Q-R{len(questions) + 1}"
        questions.append(Question(id=qid, text=f"How do the KPIs break down by {dimension}?", source=f"contract.dimensions[{dimension}]"))
        if not measured:
            forced[qid] = "The approved model has no measures to break down"
    return questions, forced


def derive_audience(contract: BusinessContract, subject_area: str | None) -> list[str]:
    roles = sorted({role for s in contract.security for role in s.applies_to}, key=str.casefold)
    if roles:
        return roles
    return [f"{subject_area or contract.domain or 'Business'} stakeholders"]


# --- the prompt ---------------------------------------------------------------------------------


def llm_schema() -> dict:
    nullable = {"type": ["string", "null"]}
    ref = {
        "type": "object",
        "properties": {"measure": nullable, "table": nullable, "column": nullable, "hierarchy": nullable},
        "required": ["measure", "table", "column", "hierarchy"],
        "additionalProperties": False,
    }
    refs = {"type": "array", "items": ref}
    flt = {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "column": {"type": "string"},
            "values": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
        },
        "required": ["table", "column", "values"],
        "additionalProperties": False,
    }
    strings = {"type": "array", "items": {"type": "string"}}
    visual = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": list(VISUAL_TYPES)},
            "title": {"type": "string"},
            "question_ids": strings,
            "fields": {
                "type": "object",
                "properties": {"category": refs, "series": refs, "values": refs},
                "required": ["category", "series", "values"],
                "additionalProperties": False,
            },
            "filters": {"type": "array", "items": flt},
            "drill": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"hierarchy": {"type": "string"}, "table": nullable},
                        "required": ["hierarchy", "table"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["id", "type", "title", "question_ids", "fields", "filters", "drill"],
        "additionalProperties": False,
    }
    page = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "purpose": {"type": "string"},
            "visuals": {"type": "array", "items": visual},
            "filters": {"type": "array", "items": flt},
        },
        "required": ["name", "purpose", "visuals", "filters"],
        "additionalProperties": False,
    }
    interaction = {
        "type": "object",
        "properties": {
            "from_visual": {"type": "string"},
            "to_visual": {"type": "string"},
            "type": {"type": "string", "enum": list(INTERACTION_TYPES)},
        },
        "required": ["from_visual", "to_visual", "type"],
        "additionalProperties": False,
    }
    unanswered = {
        "type": "object",
        "properties": {"question_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["question_id", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "pages": {"type": "array", "items": page},
            "interactions": {"type": "array", "items": interaction},
            "unanswered_questions": {"type": "array", "items": unanswered},
        },
        "required": ["pages", "interactions", "unanswered_questions"],
        "additionalProperties": False,
    }


def default_report_llm() -> LlmCall:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


def build_payload(
    contract: BusinessContract, spec: ModelSpec, questions: list[Question], audience: list[str], desired_outputs: list[str]
) -> dict:
    """The contract, the questions and the model's names, types and descriptions. No data values."""
    return {
        "business_objective": contract.business_objective,
        "audience": audience,
        "desired_outputs": desired_outputs,
        "kpis": [
            {"name": k.name, "formula_intent": k.formula_intent, "grain": k.grain, "time_logic": k.time_logic}
            for k in contract.kpis
        ],
        "dimensions": contract.dimensions,
        "questions": [{"id": q.id, "text": q.text, "source": q.source} for q in questions],
        "measures": [
            {"name": m.name, "table": m.table, "kpi": m.kpi, "format_string": m.format_string, "description": m.description}
            for m in sorted(spec.measures, key=lambda m: m.name)
            if m.dax
        ],
        "tables": [
            {
                "name": t.name,
                "role": t.role,
                "columns": [{"name": c.name, "data_type": c.data_type} for c in sorted(t.columns, key=lambda c: c.name) if not c.is_hidden],
                "hierarchies": [{"name": h.name, "levels": [lv.name for lv in h.levels]} for h in sorted(t.hierarchies, key=lambda h: h.name)],
            }
            for t in sorted(spec.tables, key=lambda t: t.name)
        ],
    }


def build_user_prompt(payload: dict, errors: list[str] | None = None) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if errors:
        text += "\n\nYour previous answer was rejected for these reasons. Fix them and answer again:\n" + "\n".join(
            f"- {e}" for e in errors[:30]
        )
    return text


# --- proposing and checking -----------------------------------------------------------------------


def _candidate(raw: str, base: ReportPlan) -> tuple[ReportPlan | None, list[str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, ["the answer is not valid JSON"]
    if not isinstance(data, dict):
        return None, ["the answer is not a JSON object"]
    try:
        candidate = ReportPlan.model_validate(
            {
                **base.model_dump(mode="json"),
                "pages": data.get("pages", []),
                "interactions": data.get("interactions", []),
                "unanswered_questions": [
                    *(u.model_dump() for u in base.unanswered_questions),
                    *(u for u in data.get("unanswered_questions", []) if isinstance(u, dict)),
                ],
            }
        )
    except ValueError as exc:
        return None, [f"the answer does not match the schema: {exc}"]
    return candidate, []


def _finalize(plan: ReportPlan, forced: dict[str, str]) -> ReportPlan:
    """Stable ids `v<n>` in page order, interactions remapped, and unanswered = forced + declared
    questions that really have no visual."""
    mapping: dict[str, str] = {}
    for page in plan.pages:
        for visual in page.visuals:
            mapping[visual.id] = f"v{len(mapping) + 1}"
    for page in plan.pages:
        for visual in page.visuals:
            visual.id = mapping[visual.id]
            visual.question_ids = list(dict.fromkeys(visual.question_ids))
    plan.interactions = [Interaction(from_visual=mapping[i.from_visual], to_visual=mapping[i.to_visual], type=i.type) for i in plan.interactions]
    covered = {q for p in plan.pages for v in p.visuals for q in v.question_ids}
    declared = {u.question_id: u.reason for u in plan.unanswered_questions}
    plan.unanswered_questions = [
        UnansweredQuestion(question_id=q.id, reason=forced.get(q.id) or declared[q.id])
        for q in plan.questions
        if q.id in forced or (q.id in declared and q.id not in covered)
    ]
    return plan


def propose_report_plan(
    contract: BusinessContract,
    spec: ModelSpec,
    llm: LlmCall,
    run_id: str,
    subject_area: str | None = None,
    desired_outputs: list[str] | None = None,
) -> ReportPlan:
    """A validated ReportPlan (unversioned, `based_on` empty). Raises LlmInvalidOutput if the
    model's answer is still unusable after MAX_ATTEMPTS."""
    questions, forced = derive_questions(contract, spec)
    audience = derive_audience(contract, subject_area)
    base = ReportPlan(
        run_id=run_id,
        audience=audience,
        questions=questions,
        unanswered_questions=[UnansweredQuestion(question_id=q, reason=r) for q, r in forced.items()],
    )
    if len(forced) == len(questions):  # nothing can be answered: don't ask the model for a layout
        return base
    askable = [q for q in questions if q.id not in forced]
    payload = build_payload(contract, spec, askable, audience, desired_outputs or [])
    schema = llm_schema()
    errors: list[str] = []
    attempts: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = llm(SYSTEM_PROMPT, build_user_prompt(payload, errors), schema)
        candidate, errors = _candidate(raw, base)
        if candidate is not None:
            _, result = validate_report_plan(candidate, spec)
            errors = failures(result)
            for page in candidate.pages:
                for v in page.visuals:
                    errors += [f"{v.id}: question {q} can't be answered by any approved measure; remove it from question_ids" for q in v.question_ids if q in forced]
            if not errors:
                return _finalize(candidate, forced)
        attempts.append({"attempt": attempt, "raw": raw, "errors": errors})
    raise LlmInvalidOutput("The model's report plan was unusable: " + "; ".join(errors[:5]), attempts)
