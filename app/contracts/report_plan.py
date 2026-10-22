"""ReportPlan.json (T-601, FR-013, spec §10): the pages, visuals and filters of the report.

The plan only names objects of the approved ModelSpec; `report/plan_validation.py` checks that.
`extra="forbid"` means a typo in a hand-edited file is an error. Files are saved with a fixed key
order, UTF-8 and LF line endings.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
VISUAL_TYPES = ("card", "lineChart", "clusteredBarChart", "clusteredColumnChart", "tableEx", "pivotTable", "slicer")
INTERACTION_TYPES = ("filter", "highlight", "none")
FIELD_ROLES = ("category", "series", "values")  # pivotTable: rows, columns, values


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldRef(_Strict):
    """One bound object: a measure, a column (`table` + `column`) or a hierarchy (`table` + `hierarchy`)."""

    measure: str | None = None
    table: str | None = None
    column: str | None = None
    hierarchy: str | None = None


class VisualFields(_Strict):
    category: list[FieldRef] = Field(default_factory=list)
    series: list[FieldRef] = Field(default_factory=list)
    values: list[FieldRef] = Field(default_factory=list)


class Filter(_Strict):
    table: str
    column: str
    values: list[str | int | float] = Field(default_factory=list)


class Drill(_Strict):
    hierarchy: str
    table: str | None = None


class Visual(_Strict):
    id: str
    type: str  # checked against VISUAL_TYPES by the validation, so the failure lists them
    title: str
    question_ids: list[str] = Field(default_factory=list)
    fields: VisualFields = Field(default_factory=VisualFields)
    filters: list[Filter] = Field(default_factory=list)
    drill: Drill | None = None


class Page(_Strict):
    name: str
    purpose: str
    visuals: list[Visual] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)


class Question(_Strict):
    id: str
    text: str
    source: str


class Interaction(_Strict):
    from_visual: str
    to_visual: str
    type: str


class UnansweredQuestion(_Strict):
    question_id: str
    reason: str


class ReportPlan(_Strict):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int = Field(default=1, ge=1)
    based_on: dict[str, str] = Field(default_factory=dict)
    audience: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)
    interactions: list[Interaction] = Field(default_factory=list)
    unanswered_questions: list[UnansweredQuestion] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_data(self)


def to_data(plan: ReportPlan) -> dict[str, Any]:
    """Plain data with unused field-reference keys dropped, so the file reads like the spec's example."""
    return _compact(plan.model_dump(mode="json"))


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"measure", "table", "column", "hierarchy"}:  # a FieldRef
            return {k: v for k, v in value.items() if v is not None}
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value
