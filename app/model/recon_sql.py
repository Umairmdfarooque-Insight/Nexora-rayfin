"""Independent reconciliation SQL for Gate G4 (T-504, business rule 1).

The figure a measure is checked against must not come from the measure's own DAX. The model
writes T-SQL for the same KPI from the approved definition and the source tables' names and types
(it never sees the DAX or any data). Code then refuses anything that isn't a single read-only
SELECT over inventory tables, or that hardcodes a value the definition doesn't state. The query
is run on the Lakehouse SQL endpoint and shown for review next to the DAX value.
"""

import json
import re
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.business_contract import Kpi
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.model_plan import ModelSpec
from app.model.dax_generator import ALLOWED_NUMBERS, definition_text

SCHEMA_NAME = "reconciliation_sql"
MAX_ATTEMPTS = 2
LlmCall = Callable[[str, str, dict], str]

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE|DENY|INTO|"
    r"OPENROWSET|OPENQUERY|OPENDATASOURCE|BULK|WAITFOR|SHUTDOWN|DBCC|USE)\b|\bxp_\w+",
    re.IGNORECASE,
)
IDENT = r"(?:\[(?:[^\]]|\]\])+\]|`[^`]+`|[A-Za-z_]\w*)"  # [T-SQL], `Spark` or a plain identifier
SOURCE_REF = re.compile(r"\b(?:FROM|JOIN)\s+(" + IDENT + r"(?:\s*\.\s*" + IDENT + r")*)", re.IGNORECASE)
NUMBER = re.compile(r"(?<![A-Za-z_\d.\[`])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
PART = re.compile(r"\[((?:[^\]]|\]\])+)\]|`([^`]+)`|([A-Za-z_]\w*)")

SYSTEM_PROMPT = """You are Nexora's reconciliation analyst for Microsoft Fabric analytics.
Write T-SQL that computes the SAME business KPI straight from the source tables, so its result can
be compared with a Power BI measure. You are given the approved KPI definition and the source
tables (names and types only, never data) with the joins the model uses.

Rules:
1. total_sql returns exactly one row and one column named value: the KPI over all the data.
2. If one of the model's dimension columns can slice the KPI, set slice_table and slice_column to
   that model table and column (model names as listed) and give slice_sql: two columns, label
   (the source column behind that dimension column) and value (the KPI per label), one row per
   label. Otherwise set slice_table, slice_column and slice_sql to null.
3. Read-only: one SELECT (or WITH ... SELECT) statement. No INSERT, UPDATE, DELETE, DDL, EXEC or INTO.
4. Use only the listed source tables as [schema].[table] and only their listed source columns.
5. Implement exactly the definition: its formula intent, filters, time logic and exclusions. Do not
   add business logic. Do not hardcode a number or text unless the definition states that exact value.
6. Do not write DAX. Return JSON only."""


class ReconSql(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_sql: str
    slice_table: str | None = None
    slice_column: str | None = None
    slice_sql: str | None = None
    explanation: str


class ReconSqlRejected(Exception):
    def __init__(self, reason: str, attempts: list[dict] | None = None):
        self.reason = reason
        self.attempts = attempts or []
        super().__init__(reason)


def llm_schema() -> dict:
    nullable = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "total_sql": {"type": "string"},
            "slice_table": nullable,
            "slice_column": nullable,
            "slice_sql": nullable,
            "explanation": {"type": "string"},
        },
        "required": ["total_sql", "slice_table", "slice_column", "slice_sql", "explanation"],
        "additionalProperties": False,
    }


def default_recon_llm() -> LlmCall:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


def source_key(table) -> str:
    return f"{table.source.schema_name}.{table.source.table}"


def build_payload(kpi: Kpi, spec: ModelSpec) -> dict:
    tables = [t for t in sorted(spec.tables, key=lambda t: t.name) if t.source is not None]
    by_name = {t.name: t for t in tables}

    def source_column(table_name: str, column: str) -> str:
        t = by_name.get(table_name)
        return next((c.source_column for c in t.columns if c.name == column), column) if t else column

    return {
        "kpi": {
            "name": kpi.name,
            "formula_intent": kpi.formula_intent,
            "grain": kpi.grain,
            "filters": kpi.filters,
            "time_logic": kpi.time_logic,
            "exclusions": kpi.exclusions,
        },
        "tables": [
            {
                "model_table": t.name,
                "source": source_key(t),
                "role": t.role,
                "columns": [
                    {"model_column": c.name, "source_column": c.source_column, "data_type": c.data_type}
                    for c in sorted(t.columns, key=lambda c: c.name)
                ],
            }
            for t in tables
        ],
        "joins": sorted(
            f"{source_key(by_name[r.from_.table])}.{source_column(r.from_.table, r.from_.column)} = "
            f"{source_key(by_name[r.to.table])}.{source_column(r.to.table, r.to.column)}"
            for r in spec.relationships
            if r.from_.table in by_name and r.to.table in by_name
        ),
    }


def build_user_prompt(kpi: Kpi, spec: ModelSpec, errors: list[str] | None = None) -> str:
    text = json.dumps(build_payload(kpi, spec), sort_keys=True, ensure_ascii=False)
    if errors:
        text += "\n\nYour previous answer was rejected for these reasons. Fix them and answer again:\n" + "\n".join(
            f"- {e}" for e in errors
        )
    return text


def _strip(sql: str) -> tuple[str, list[str]]:
    """The SQL without comments, string literals blanked, and the literals."""
    out, strings, i, n = [], [], 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if two == "--":
            while i < n and sql[i] not in "\r\n":
                i += 1
        elif two == "/*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif sql[i] == "'":
            j, value = i + 1, []
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1 : j + 2] == "'":
                        value.append("'")
                        j += 2
                        continue
                    break
                value.append(sql[j])
                j += 1
            strings.append("".join(value))
            out.append(" '' ")
            i = j + 1
        elif sql[i] == "[":  # a bracketed identifier is kept whole
            j = i + 1
            while j < n:
                if sql[j] == "]":
                    if sql[j + 1 : j + 2] == "]":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
        elif sql[i] == "`":  # so is a Spark identifier in backticks
            j = sql.find("`", i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i : j + 1])
            i = j + 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out), strings


def check_sql(sql: str, inventory: MetadataInventory, kpi: Kpi, label: str) -> list[str]:
    """Every problem with a reference query. Empty means it may be run."""
    if not sql or not sql.strip():
        return [f"{label} is empty"]
    clean, strings = _strip(sql)
    body = clean.strip().rstrip(";").strip()
    errors: list[str] = []
    if ";" in body:
        errors.append(f"{label} must be a single statement")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", body):
        errors.append(f"{label} must start with SELECT or WITH")
    forbidden = sorted({m.group(0).upper() for m in FORBIDDEN.finditer(body)})
    if forbidden:
        errors.append(f"{label} may only read data: {', '.join(forbidden)} is not allowed")

    known = {f"{t.schema_name}.{t.table_name}".casefold() for t in inventory.tables}
    ctes = {m.group(1).casefold() for m in re.finditer(r"(?is)(?:\bWITH\b|,)\s*(\w+)\s+AS\s*\(", body)}
    for m in SOURCE_REF.finditer(body):
        parts = [(a or b or c).replace("]]", "]") for a, b, c in PART.findall(m.group(1))]
        if len(parts) == 1 and parts[0].casefold() in ctes:
            continue
        if len(parts) != 2 or f"{parts[0]}.{parts[1]}".casefold() not in known:
            errors.append(f"{label} reads {'.'.join(parts)}, which is not a [schema].[table] of the inventory")

    allowed = set(ALLOWED_NUMBERS) | {float(n) for n in re.findall(r"\d+(?:\.\d+)?", definition_text(kpi))}
    for literal in NUMBER.findall(body):
        if float(literal) not in allowed:
            errors.append(f"{label} hardcodes {literal}: the KPI definition doesn't state it")
    states = definition_text(kpi).casefold()
    for text in strings:
        if text.strip() and text.casefold() not in states:
            errors.append(f"{label} hardcodes the text {text!r}: the KPI definition doesn't state it")
    return errors


def parse_recon(raw: str, kpi: Kpi, spec: ModelSpec, inventory: MetadataInventory) -> tuple[ReconSql | None, list[str]]:
    try:
        recon = ReconSql.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None, ["the answer is not valid JSON in the requested shape"]
    errors = check_sql(recon.total_sql, inventory, kpi, "total_sql")
    if recon.slice_sql or recon.slice_table or recon.slice_column:
        if not (recon.slice_sql and recon.slice_table and recon.slice_column):
            errors.append("slice_table, slice_column and slice_sql must be given together, or all be null")
        else:
            table = next((t for t in spec.tables if t.name.casefold() == recon.slice_table.casefold()), None)
            if table is None or recon.slice_column.casefold() not in {c.name.casefold() for c in table.columns}:
                errors.append(f"the slice {recon.slice_table}[{recon.slice_column}] is not a column of the model")
            errors += check_sql(recon.slice_sql, inventory, kpi, "slice_sql")
    return (None, errors) if errors else (recon, [])


def propose_recon_sql(kpi: Kpi, spec: ModelSpec, inventory: MetadataInventory, llm: LlmCall) -> ReconSql:
    """The accepted reference queries, or `ReconSqlRejected` after at most 2 attempts."""
    errors: list[str] = []
    log: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = llm(SYSTEM_PROMPT, build_user_prompt(kpi, spec, errors), llm_schema())
        recon, errors = parse_recon(raw, kpi, spec, inventory)
        log.append({"attempt": attempt, "raw": raw, "errors": errors})
        if recon is not None:
            return recon
    raise ReconSqlRejected("; ".join(errors), log)
