"""DAX measures from approved KPI definitions (T-503, FR-011, business rules 1-2).

The model drafts DAX from the KPI's formula intent, grain, filters, time logic and exclusions plus
the ModelSpec's object names and types (never data values). Its answer is only a candidate: code
extracts every `Table[Column]` and `[Measure]` from the DAX text, and the proposal is rejected if a
reference is missing from the ModelSpec, if the model's own `referenced_objects` list disagrees with
the text, or if it breaks a rule that can be checked (the `/` operator instead of DIVIDE, a
hardcoded literal the KPI definition doesn't state, time intelligence without a date table).
"""

import json
import re
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.business_contract import Kpi
from app.contracts.model_plan import ModelSpec

SCHEMA_NAME = "dax_measure"
MAX_ATTEMPTS = 2
LlmCall = Callable[[str, str, dict], str]

TIME_FUNCTIONS = (
    "SAMEPERIODLASTYEAR DATEADD DATESYTD DATESMTD DATESQTD TOTALYTD TOTALMTD TOTALQTD PARALLELPERIOD "
    "PREVIOUSYEAR PREVIOUSQUARTER PREVIOUSMONTH PREVIOUSDAY NEXTYEAR NEXTQUARTER NEXTMONTH NEXTDAY "
    "DATESBETWEEN DATESINPERIOD OPENINGBALANCEYEAR OPENINGBALANCEQUARTER OPENINGBALANCEMONTH "
    "CLOSINGBALANCEYEAR CLOSINGBALANCEQUARTER CLOSINGBALANCEMONTH STARTOFYEAR ENDOFYEAR "
    "STARTOFQUARTER ENDOFQUARTER STARTOFMONTH ENDOFMONTH"
).split()
TIME_FUNCTION = re.compile(r"\b(" + "|".join(TIME_FUNCTIONS) + r")\s*\(", re.IGNORECASE)
KEYWORDS = {"RETURN", "VAR", "IN", "NOT", "AND", "OR", "ASC", "DESC", "TRUE", "FALSE"}
ALLOWED_NUMBERS = {0.0, 1.0}

SYSTEM_PROMPT = """You are Nexora's DAX author for Power BI semantic models on Microsoft Fabric.
Write one DAX measure expression that implements EXACTLY the approved KPI definition you are given:
its formula intent, grain, filters, time logic and exclusions. Use only the tables, columns and
measures listed in the model, and nothing else.

Rules:
1. Write the expression only: no `measure Name =` prefix, no DEFINE or EVALUATE.
2. Qualify every column as 'Table'[Column], with the table name exactly as listed. Refer to another
   measure as [Measure Name]. Never use an unqualified column.
3. Use DIVIDE() for ratios. Never use the / operator.
4. Do not hardcode any number or text unless the KPI definition states that exact value. If the
   definition names a value you can't see in the model, don't guess it: use only what is stated.
5. Apply every filter and exclusion of the definition. Do not add business logic that isn't in it.
6. List every table column and measure the expression uses in referenced_objects, one entry each
   (table and column for a column, measure for a measure, null for the unused fields).
7. You see names and types only, never data. Return JSON only."""


class DaxRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str | None = None
    column: str | None = None
    measure: str | None = None


class DaxProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    measure_name: str
    dax: str
    referenced_objects: list[DaxRef]
    explanation: str


class DaxRejected(Exception):
    """The proposal can't be used. `reason` says why; no DAX is invented in its place."""

    def __init__(self, reason: str, attempts: list[dict] | None = None):
        self.reason = reason
        self.attempts = attempts or []
        super().__init__(reason)


def llm_schema() -> dict:
    optional = {"type": ["string", "null"]}
    ref = {
        "type": "object",
        "properties": {"table": optional, "column": optional, "measure": optional},
        "required": ["table", "column", "measure"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "measure_name": {"type": "string"},
            "dax": {"type": "string"},
            "referenced_objects": {"type": "array", "items": ref},
            "explanation": {"type": "string"},
        },
        "required": ["measure_name", "dax", "referenced_objects", "explanation"],
        "additionalProperties": False,
    }


def default_dax_llm() -> LlmCall:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


# --- the prompt -------------------------------------------------------------------------------


def build_payload(kpi: Kpi, spec: ModelSpec, measure_name: str) -> dict:
    """The KPI definition and the model's names and types. No data values."""
    tables = []
    for t in sorted(spec.tables, key=lambda t: t.name):
        tables.append(
            {
                "name": t.name,
                "role": t.role,
                "grain": t.grain,
                "columns": [{"name": c.name, "data_type": c.data_type} for c in sorted(t.columns, key=lambda c: c.name)],
            }
        )
    return {
        "kpi": {
            "name": kpi.name,
            "formula_intent": kpi.formula_intent,
            "grain": kpi.grain,
            "filters": kpi.filters,
            "time_logic": kpi.time_logic,
            "exclusions": kpi.exclusions,
            "currency": kpi.currency,
        },
        "measure_name": measure_name,
        "tables": tables,
        "other_measures": sorted(m.name for m in spec.measures if m.name != measure_name),
        "relationships": sorted(
            f"{r.from_.table}[{r.from_.column}] -> {r.to.table}[{r.to.column}] ({r.cardinality})" for r in spec.relationships
        ),
    }


def build_user_prompt(kpi: Kpi, spec: ModelSpec, measure_name: str, errors: list[str] | None = None) -> str:
    text = json.dumps(build_payload(kpi, spec, measure_name), sort_keys=True, ensure_ascii=False)
    if errors:
        text += "\n\nYour previous answer was rejected for these reasons. Fix them and answer again:\n" + "\n".join(
            f"- {e}" for e in errors
        )
    return text


# --- reading the DAX text ------------------------------------------------------------------------

_QUALIFIED = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_]*)\s*\[((?:[^\]]|\]\])+)\]")
_BARE = re.compile(r"\[((?:[^\]]|\]\])+)\]")
_NUMBER = re.compile(r"(?<![A-Za-z_\d.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def strip_comments_and_strings(dax: str) -> tuple[str, list[str]]:
    """The DAX without comments, with each string literal replaced by a blank, and the literals."""
    out: list[str] = []
    strings: list[str] = []
    i, n = 0, len(dax)
    while i < n:
        ch = dax[i]
        two = dax[i : i + 2]
        if two in ("//", "--"):
            while i < n and dax[i] not in "\r\n":
                i += 1
        elif two == "/*":
            end = dax.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif ch == '"':
            j, value = i + 1, []
            while j < n:
                if dax[j] == '"':
                    if dax[j + 1 : j + 2] == '"':
                        value.append('"')
                        j += 2
                        continue
                    break
                value.append(dax[j])
                j += 1
            strings.append("".join(value))
            out.append(' "" ')
            i = j + 1
        elif ch == "'":  # a quoted table name: kept whole so its characters aren't read as DAX
            j = i + 1
            while j < n:
                if dax[j] == "'":
                    if dax[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(dax[i : j + 1])
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), strings


def _unquote_table(name: str) -> str:
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


def _unbracket(name: str) -> str:
    return name.replace("]]", "]")


def extract_references(dax: str) -> tuple[list[tuple[str, str]], list[str], str]:
    """(qualified columns as (table, column), bare [names], the text left after removing them)."""
    clean, _ = strip_comments_and_strings(dax)
    columns: list[tuple[str, str]] = []
    bare: list[str] = []
    rest: list[str] = []
    pos = 0
    for m in re.finditer(_QUALIFIED.pattern + "|" + _BARE.pattern, clean):
        rest.append(clean[pos : m.start()])
        pos = m.end()
        if m.group(1) is not None and _unquote_table(m.group(1)).upper() not in KEYWORDS:
            columns.append((_unquote_table(m.group(1)), _unbracket(m.group(2))))
        elif m.group(1) is not None:  # `RETURN [Measure]` and the like: the keyword is not a table
            rest.append(m.group(1) + " ")
            bare.append(_unbracket(m.group(2)))
        else:
            bare.append(_unbracket(m.group(3)))
    rest.append(clean[pos:])
    return columns, bare, " ".join(rest)


# --- checks -----------------------------------------------------------------------------------


def date_tables(spec: ModelSpec) -> list[str]:
    return [t.name for t in spec.tables if t.role == "date"]


def definition_text(kpi: Kpi) -> str:
    return " ".join([kpi.formula_intent or "", kpi.grain or "", kpi.time_logic or "", *kpi.filters, *kpi.exclusions])


def date_table_required(kpi: Kpi, spec: ModelSpec) -> str | None:
    if (kpi.time_logic or "").strip() and not date_tables(spec):
        return f"date table required: KPI {kpi.name!r} has time logic ({kpi.time_logic.strip()!r}) but the model has no date table"
    return None


def check_dax(dax: str, referenced: list[DaxRef], kpi: Kpi, spec: ModelSpec, measure_name: str) -> list[str]:
    """Every problem with a proposed expression. Empty means it can be used."""
    errors: list[str] = []
    if not dax.strip():
        return ["the DAX is empty"]
    tables = {t.name.casefold(): t for t in spec.tables}
    measures = {m.name.casefold() for m in spec.measures}
    columns, bare, rest = extract_references(dax)

    found: set[tuple] = set()
    for table, column in columns:
        t = tables.get(table.casefold())
        if t is None:
            errors.append(f"unknown table {table!r} in {table}[{column}]")
        elif column.casefold() not in {c.name.casefold() for c in t.columns}:
            errors.append(f"unknown column {column!r} in table {t.name!r}: {table}[{column}] is not in the ModelSpec")
        else:
            found.add(("c", t.name.casefold(), column.casefold()))
    for name in bare:
        if name.casefold() == measure_name.casefold():
            errors.append(f"measure [{name}] refers to itself")
        elif name.casefold() in measures:
            found.add(("m", name.casefold()))
        else:
            holders = sorted(t.name for t in spec.tables if name.casefold() in {c.name.casefold() for c in t.columns})
            if holders:
                errors.append(f"unqualified column [{name}]: write it as 'Table'[{name}] (found in {', '.join(holders)})")
            else:
                errors.append(f"unknown measure or column [{name}]: it is not in the ModelSpec")

    listed: set[tuple] = set()
    for ref in referenced:
        if ref.measure and not ref.table and not ref.column:
            listed.add(("m", ref.measure.casefold()))
        elif ref.table and ref.column and not ref.measure:
            listed.add(("c", ref.table.casefold(), ref.column.casefold()))
        else:
            errors.append("referenced_objects has an entry that is neither a table column nor a measure")
    if not errors and listed != found:
        missing = sorted(map(str, found - listed))
        extra = sorted(map(str, listed - found))
        errors.append(
            "referenced_objects doesn't match the references in the DAX text"
            + (f" (not listed: {', '.join(missing)})" if missing else "")
            + (f" (listed but not used: {', '.join(extra)})" if extra else "")
        )

    if "/" in rest:
        errors.append("use DIVIDE() for ratios, not the / operator")

    allowed = set(ALLOWED_NUMBERS) | {float(n) for n in _NUMBER.findall(definition_text(kpi))}
    for literal in _NUMBER.findall(rest):
        if float(literal) not in allowed:
            errors.append(f"hardcoded literal {literal}: the KPI definition doesn't state it")
    states = definition_text(kpi).casefold()
    _, strings = strip_comments_and_strings(dax)
    for text in strings:
        if text.strip() and text.casefold() not in states:
            errors.append(f"hardcoded text {text!r}: the KPI definition doesn't state it")

    if TIME_FUNCTION.search(rest) and not date_tables(spec):
        errors.append("date table required: the DAX uses time intelligence but the model has no date table")
    return errors


def check_proposal(raw: str, kpi: Kpi, spec: ModelSpec, measure_name: str) -> tuple[DaxProposal | None, list[str]]:
    try:
        proposal = DaxProposal.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None, ["the answer is not valid JSON in the requested shape"]
    if proposal.measure_name.casefold() != measure_name.casefold():
        return None, [f"measure_name is {proposal.measure_name!r} but the measure is {measure_name!r}"]
    errors = check_dax(proposal.dax, proposal.referenced_objects, kpi, spec, measure_name)
    return (None, errors) if errors else (proposal, [])


def approved_description(kpi: Kpi) -> str:
    """The measure's description: the approved definition, quoted."""
    lines = [f"Approved: {kpi.formula_intent}" if kpi.formula_intent else f"Approved KPI: {kpi.name}"]
    for label, value in (
        ("Grain", kpi.grain),
        ("Filters", "; ".join(kpi.filters)),
        ("Time logic", kpi.time_logic),
        ("Exclusions", "; ".join(kpi.exclusions)),
    ):
        if value and value.strip():
            lines.append(f"{label}: {value.strip()}")
    return "\n".join(lines)


def propose_measure_dax(
    kpi: Kpi,
    spec: ModelSpec,
    llm: LlmCall | None = None,
    measure_name: str | None = None,
    feedback: list[str] | None = None,
) -> DaxProposal:
    """The accepted DAX for one measure, or `DaxRejected` with the reason after at most 2 attempts.

    `feedback` is why an earlier version failed validation (T-505), given to the model from the first attempt."""
    from app.graphs.base import run_graph
    from app.graphs.dax_measure import build_dax_measure_graph

    name = measure_name or kpi.name
    reason = date_table_required(kpi, spec)
    if reason:
        raise DaxRejected(reason)
    graph = build_dax_measure_graph(llm or default_dax_llm())
    final = run_graph(graph, {"kpi": kpi, "spec": spec, "measure": name, "errors": list(feedback or [])}, run_id=spec.run_id, stage="dax_measure")
    if final.get("proposal") is None:
        raise DaxRejected("; ".join(final.get("errors", [])) or "no usable proposal", final.get("attempts_log", []))
    return final["proposal"]
