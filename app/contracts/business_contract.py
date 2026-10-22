"""BusinessContract: the structured, versioned requirement the business owner approves (T-201).

Every later stage builds on this file, so its rules are deterministic and live here, not in a
prompt. `BusinessContract.yaml` is saved with a fixed key order, UTF-8 and LF line endings, so
saving the same contract twice gives the same bytes and the same hash.
"""

import hashlib
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1


class ContractStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class KpiStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DATA_GAP = "data_gap"


class QuestionSource(str, Enum):
    RULE = "rule"  # deterministic ambiguity rule (T-202)
    LLM = "llm"
    REVIEWER = "reviewer"  # raised when a reviewer rejects a draft (T-204)


class QuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    DISMISSED = "dismissed"  # non-blocking question closed with a reason (T-203)


class FeasibilityStatus(str, Enum):
    FEASIBLE = "feasible"
    DATA_GAP = "data_gap"


class _Strict(BaseModel):
    # Unknown fields are errors, so a typo such as `kpi:` can't pass silently.
    model_config = ConfigDict(extra="forbid")


class ExpectedExample(_Strict):
    description: str
    value: float | int | str | None = None


class Kpi(_Strict):
    name: str = Field(min_length=1)
    formula_intent: str | None = None
    grain: str | None = None
    filters: list[str] = Field(default_factory=list)
    time_logic: str | None = None
    exclusions: list[str] = Field(default_factory=list)
    currency: str | None = None
    expected_examples: list[ExpectedExample] = Field(default_factory=list)
    status: KpiStatus = KpiStatus.DRAFT
    rationale: str | None = None


class SecurityRequirement(_Strict):
    description: str
    applies_to: list[str] = Field(default_factory=list)


class OpenQuestion(_Strict):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    blocking: bool = False
    reason: str | None = None
    source: QuestionSource = QuestionSource.RULE
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    dismiss_reason: str | None = None


class LockedFact(_Strict):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_question_id: str | None = None
    locked_at: str  # ISO 8601 timestamp, kept as text so it round-trips exactly


class FeasibilityNeed(_Strict):
    need: str
    mapped: str | None = None


class FeasibilityMissing(_Strict):
    need: str
    reason: str


class Feasibility(_Strict):
    """Filled in by T-402. `data_gap` means no calculation may be produced for the KPI."""

    kpi: str
    status: FeasibilityStatus
    needed: list[FeasibilityNeed] = Field(default_factory=list)
    missing: list[FeasibilityMissing] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)


class BusinessContract(_Strict):
    schema_version: int = SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    status: ContractStatus = ContractStatus.DRAFT
    domain: str | None = None
    business_objective: str = Field(min_length=1)
    grain: str | None = None
    entities: list[str] = Field(default_factory=list)
    kpis: list[Kpi] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    currency: str | None = None
    exclusions: list[str] = Field(default_factory=list)
    security: list[SecurityRequirement] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    locked_facts: list[LockedFact] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    feasibility: list[Feasibility] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> "BusinessContract":
        issues = rule_issues(self)
        if issues:
            # One "path: message" line per problem; `check()` turns them into issues.
            raise ValueError("\n".join(issues))
        return self


def rule_issues(contract: BusinessContract) -> list[str]:
    """The cross-field rules, as `path: message` lines."""
    issues: list[str] = []

    first_kpi: dict[str, int] = {}
    for index, kpi in enumerate(contract.kpis):
        key = kpi.name.strip().lower()
        if key in first_kpi:
            other = contract.kpis[first_kpi[key]].name
            issues.append(
                f"kpis.{index}.name: duplicate KPI name {kpi.name!r} "
                f"(same as kpis.{first_kpi[key]}.name {other!r})"
            )
        else:
            first_kpi[key] = index

    for label, ids in (
        ("open_questions", [q.id for q in contract.open_questions]),
        ("locked_facts", [f.id for f in contract.locked_facts]),
    ):
        seen: set[str] = set()
        for index, value in enumerate(ids):
            if value in seen:
                issues.append(f"{label}.{index}.id: duplicate id {value!r}")
            seen.add(value)

    for index, question in enumerate(contract.open_questions):
        if question.blocking and question.status is QuestionStatus.DISMISSED:
            issues.append(
                f"open_questions.{index}.status: blocking question {question.id} "
                "can't be dismissed"
            )

    question_ids = {q.id for q in contract.open_questions}
    for index, fact in enumerate(contract.locked_facts):
        if fact.source_question_id and fact.source_question_id not in question_ids:
            issues.append(
                f"locked_facts.{index}.source_question_id: "
                f"no question {fact.source_question_id!r}"
            )

    if contract.status is ContractStatus.APPROVED:
        for index, question in enumerate(contract.open_questions):
            if question.blocking and question.status is QuestionStatus.OPEN:
                issues.append(
                    f"open_questions.{index}: blocking question {question.id} is still open, "
                    "so the contract can't be approved"
                )
        for index, kpi in enumerate(contract.kpis):
            for field in ("formula_intent", "grain", "currency"):
                if not (getattr(kpi, field) or "").strip():
                    issues.append(
                        f"kpis.{index}.{field}: required when the contract is approved"
                    )
    return issues


def open_blocking_questions(contract: BusinessContract) -> list[OpenQuestion]:
    return [
        q
        for q in contract.open_questions
        if q.blocking and q.status is QuestionStatus.OPEN
    ]


# --- validation, loading and saving -------------------------------------------------------


class ContractInvalid(Exception):
    """The document isn't a valid BusinessContract. `issues` has one `path: message` per error."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("\n".join(issues))


def _path(loc: tuple) -> str:
    return ".".join(str(part) for part in loc)


def check(data: Any) -> tuple[BusinessContract | None, list[str]]:
    """Validate a parsed document. Returns the contract, or None with the list of issues."""
    if not isinstance(data, dict):
        return None, ["document must be a mapping of fields"]

    version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        return None, [f"unsupported schema_version {version}"]

    try:
        return BusinessContract.model_validate(data), []
    except ValidationError as exc:
        issues: list[str] = []
        for error in exc.errors():
            where = _path(error["loc"])
            if error["type"] == "extra_forbidden":
                message = "extra field not permitted"
            elif error["type"] == "value_error" and not where:
                # The cross-field rules raise "path: message" lines themselves.
                issues.extend(error["msg"].removeprefix("Value error, ").splitlines())
                continue
            else:
                message = error["msg"]
            issues.append(f"{where}: {message}" if where else message)
        return None, issues


def parse(text: str) -> BusinessContract:
    """Parse YAML text. Raises ContractInvalid with readable messages."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise ContractInvalid([f"{where}{problem}"]) from None
    if data is None:
        raise ContractInvalid(["empty document"])
    contract, issues = check(data)
    if contract is None:
        raise ContractInvalid(issues)
    return contract


def load(path: str | Path) -> BusinessContract:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ContractInvalid(["file is not valid UTF-8"]) from None
    return parse(text)


def to_yaml(contract: BusinessContract) -> str:
    # Field order is the model's order, so output is stable.
    return yaml.safe_dump(
        contract.model_dump(mode="json"),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def save(contract: BusinessContract, path: str | Path) -> str:
    """Write the contract as YAML (UTF-8, LF) and return its `sha256:…` hash."""
    data = to_yaml(contract).encode("utf-8")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)  # bytes, so Windows doesn't turn LF into CRLF
    tmp.replace(target)
    return content_hash(data)


def json_schema() -> dict:
    """JSON Schema for LLM structured output (T-202) and external validation."""
    return BusinessContract.model_json_schema()


__all__ = [
    "BusinessContract",
    "Kpi",
    "OpenQuestion",
    "LockedFact",
    "Feasibility",
    "ContractInvalid",
    "load",
    "save",
    "parse",
    "check",
    "json_schema",
    "open_blocking_questions",
    "QuestionStatus",
]
