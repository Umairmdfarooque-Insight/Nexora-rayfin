"""Regenerating one visual or one page of the report plan from a reviewer's guidance (T-604).

The model proposes a replacement for the targeted visual (or the visuals of the targeted page) given
the reviewer's comment; code validates the whole plan with the same RPLAN checks as the planner, so
a replacement can only bind objects of the approved ModelSpec and must keep every question
answered. Everything outside the target stays as it was: a replaced visual keeps its id and its
place, and the other visuals are not sent as something to change. One retry carries the failures.
"""

import json

from app.contracts.business_contract import BusinessContract
from app.contracts.model_plan import ModelSpec
from app.contracts.report_plan import Page, ReportPlan, Visual
from app.llm.errors import LlmInvalidOutput
from app.report.plan_validation import failures, validate_report_plan
from app.report.planner import LlmCall, MAX_ATTEMPTS, build_payload, llm_schema

SCHEMA_NAME = "report_regenerate"

SYSTEM_PROMPT = """You are Nexora's report planner. A reviewer asks to change ONE part of an existing
Power BI report plan. You get the business contract, the questions, the approved semantic model's
objects, the part to replace and the reviewer's guidance. Return only the replacement.

Rules:
1. Follow the reviewer's guidance, and keep answering the questions the part answered (question_ids).
2. Use only the tables, columns, hierarchies and measures listed in the model. Never invent a measure
   or a column. Values of a visual must be measures; categories and series are columns or hierarchies.
3. Refer to a measure as {"measure": "<name>"}, a column as {"table": "<table>", "column": "<column>"}
   and a hierarchy as {"table": "<table>", "hierarchy": "<name>"}; set every unused key to null.
4. Allowed visual types: card, lineChart, clusteredBarChart, clusteredColumnChart, tableEx, pivotTable,
   slicer. If the guidance asks for something the model can't support, do the closest supported thing.
5. You see names and types only, never data. Return JSON only."""


class RegenerationFailed(LlmInvalidOutput):
    """The model's replacement was unusable after the allowed attempts; the plan is unchanged."""


def _schemas() -> tuple[dict, dict]:
    page = llm_schema()["properties"]["pages"]["items"]
    visual = page["properties"]["visuals"]["items"]
    return (
        {"type": "object", "properties": {"visual": visual}, "required": ["visual"], "additionalProperties": False},
        {"type": "object", "properties": {"page": page}, "required": ["page"], "additionalProperties": False},
    )


def find_target(plan: ReportPlan, page: str | None, visual: str | None) -> tuple[int, int | None] | None:
    """The (page index, visual index) of the target, or None. `page` matches the page's name or its
    folder name (p1, p2, …); `visual` is a visual id. A page alone targets the whole page."""
    for i, p in enumerate(plan.pages):
        if page is not None and page not in (p.name, f"p{i + 1}"):
            continue
        if visual is None:
            return (i, None) if page is not None else None
        for j, v in enumerate(p.visuals):
            if v.id == visual:
                return i, j
    return None


def _ask(plan: ReportPlan, spec: ModelSpec, contract: BusinessContract, llm: LlmCall, part: dict, guidance: str, schema: dict, build) -> ReportPlan:
    questions = plan.questions
    payload = {
        **build_payload(contract, spec, [q for q in questions if q.id not in {u.question_id for u in plan.unanswered_questions}], plan.audience, []),
        "part_to_replace": part,
        "reviewer_guidance": guidance,
    }
    errors: list[str] = []
    attempts: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if errors:
            user += "\n\nYour previous answer was rejected for these reasons. Fix them and answer again:\n" + "\n".join(f"- {e}" for e in errors[:20])
        raw = llm(SYSTEM_PROMPT, user, schema)
        try:
            candidate = build(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            errors = [f"the answer does not match the schema: {exc}"]
        else:
            _, result = validate_report_plan(candidate, spec)
            errors = failures(result)
            if not errors:
                return candidate
        attempts.append({"attempt": attempt, "raw": raw, "errors": errors})
    raise RegenerationFailed("The model's replacement was unusable: " + "; ".join(errors[:5]), attempts)


def regenerate_visual(plan: ReportPlan, spec: ModelSpec, contract: BusinessContract, llm: LlmCall, page_i: int, visual_j: int, guidance: str) -> ReportPlan:
    """A copy of `plan` where one visual is replaced (same id, same place)."""
    old = plan.pages[page_i].visuals[visual_j]
    schema, _ = _schemas()

    def build(data: dict) -> ReportPlan:
        new = Visual.model_validate({**data["visual"], "id": old.id})
        candidate = plan.model_copy(deep=True)
        candidate.pages[page_i].visuals[visual_j] = new
        return candidate

    return _ask(plan, spec, contract, llm, {"page": plan.pages[page_i].name, "visual": old.model_dump(mode="json")}, guidance, schema, build)


def regenerate_page(plan: ReportPlan, spec: ModelSpec, contract: BusinessContract, llm: LlmCall, page_i: int, guidance: str) -> ReportPlan:
    """A copy of `plan` where one page is replaced: its name stays, its visuals keep their ids in order
    and extra ones get new ids. Interactions of removed visuals are dropped."""
    old = plan.pages[page_i]
    _, schema = _schemas()
    taken = {v.id for p in plan.pages for v in p.visuals}

    def build(data: dict) -> ReportPlan:
        raw = Page.model_validate({**data["page"], "name": old.name})
        pool = [v.id for v in old.visuals]
        top = max([int(i[1:]) for i in taken if i[1:].isdigit()] + [0])
        visuals = []
        for n, v in enumerate(raw.visuals):
            if n < len(pool):
                vid = pool[n]
            else:
                top += 1
                vid = f"v{top}"
            visuals.append(v.model_copy(update={"id": vid}))
        candidate = plan.model_copy(deep=True)
        candidate.pages[page_i] = raw.model_copy(update={"visuals": visuals})
        kept = {v.id for p in candidate.pages for v in p.visuals}
        candidate.interactions = [i for i in candidate.interactions if i.from_visual in kept and i.to_visual in kept]
        return candidate

    return _ask(plan, spec, contract, llm, {"page": old.model_dump(mode="json")}, guidance, schema, build)
