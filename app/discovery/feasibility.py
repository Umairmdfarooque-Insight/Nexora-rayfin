"""KPI feasibility (T-402, business rule 8): can the mapped data support each approved KPI?

The model only extracts what a KPI needs (from its formula intent, filters, time logic and
exclusions) and says which mapped column could supply each need. Code has the final say: a need
is met only if the column it names is one SourceResolution maps and the inventory still has it.
Anything else is a data gap. A data_gap KPI gets no proxy or partial measure, ever.
"""

import json
from dataclasses import dataclass

from app.contracts.business_contract import (
    Feasibility,
    FeasibilityMissing,
    FeasibilityNeed,
    FeasibilityStatus,
)
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.source_resolution import SourceResolution
from app.discovery.data_profiler import column_kind
from app.discovery.glossary import normalize
from app.llm.errors import LlmInvalidOutput

SCHEMA_NAME = "kpi_needs"
ROLES = ("measure", "filter", "time", "dimension")
GAP_OPTIONS = ["drop_kpi", "provide_source", "data_engineering_task"]

SYSTEM_PROMPT = """You are Nexora's KPI analyst for Microsoft Fabric analytics.
For each KPI, list every field its calculation needs: what the formula intent measures, each
filter, the time logic and each exclusion. Give each need a short phrase ("line amount", "order
status", "order date") and a role (measure, filter, time, dimension). Then say which of the
available columns could supply it, as "schema.table.column" copied exactly from the list, or null
if none of them could.

Rules:
1. Include every need, even if no available column fits. Never leave a need out because it can't be met.
2. Map a need only to a column from available_columns. Never invent a column.
3. Do not propose a substitute or approximate measure. Do not generate SQL or DAX. Return JSON only."""


def llm_schema() -> dict:
    need = {
        "type": "object",
        "properties": {
            "need": {"type": "string"},
            "role": {"type": "string", "enum": list(ROLES)},
            "mapped_to": {"type": ["string", "null"]},
        },
        "required": ["need", "role", "mapped_to"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"kpi": {"type": "string"}, "needs": {"type": "array", "items": need}},
                    "required": ["kpi", "needs"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["kpis"],
        "additionalProperties": False,
    }


def available_columns(resolution: SourceResolution, inventory: MetadataInventory) -> dict[str, str | None]:
    """"schema.table.column" -> data type, for the columns SourceResolution maps that the
    inventory has. Only these can satisfy a need."""
    types = {
        f"{t.schema_name}.{t.table_name}.{c.name}".lower(): (f"{t.schema_name}.{t.table_name}.{c.name}", c.data_type)
        for t in inventory.tables
        for c in t.columns
    }
    out: dict[str, str | None] = {}
    for kpi in resolution.kpi_fields:
        for f in kpi.fields:
            found = types.get(f"{f.table}.{f.column}".lower())
            if found is not None:
                out[found[0]] = found[1]
    return dict(sorted(out.items()))


def build_payload(contract, columns: dict[str, str | None]) -> dict:
    """What the model sees: the KPI definitions and the mapped column names and types."""
    return {
        "kpis": [
            {
                "name": k.name,
                "formula_intent": k.formula_intent,
                "filters": k.filters,
                "time_logic": k.time_logic,
                "exclusions": k.exclusions,
            }
            for k in contract.kpis
        ],
        "available_columns": [{"column": name, "type": data_type} for name, data_type in columns.items()],
    }


@dataclass
class Need:
    need: str
    role: str
    mapped_to: str | None


def parse_needs(raw: str, contract) -> dict[str, list[Need]]:
    """KPI name (lower case) -> needs. Raises LlmInvalidOutput if the answer isn't usable."""
    try:
        items = json.loads(raw)["kpis"]
        if not isinstance(items, list):
            raise TypeError("kpis must be a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LlmInvalidOutput(f"The KPI needs are unusable: {exc}") from None
    wanted = {k.name.lower() for k in contract.kpis}
    out: dict[str, list[Need]] = {}
    for item in items:
        try:
            kpi, needs = str(item["kpi"]).strip().lower(), item["needs"]
            if kpi not in wanted:
                continue
            if not isinstance(needs, list):
                raise TypeError("needs must be a list")
            parsed = []
            for n in needs:
                need, role, mapped = str(n["need"]).strip(), str(n["role"]), n["mapped_to"]
                if not need or role not in ROLES or not (mapped is None or isinstance(mapped, str)):
                    raise ValueError(f"bad need {n!r}")
                parsed.append(Need(need, role, mapped.strip() if mapped else None))
        except (KeyError, TypeError, ValueError) as exc:
            raise LlmInvalidOutput(f"The KPI needs are unusable: {exc}") from None
        out.setdefault(kpi, []).extend(parsed)
    return out


def evaluate(
    contract, resolution: SourceResolution, columns: dict[str, str | None], needs: dict[str, list[Need]]
) -> list[Feasibility]:
    """Pure and deterministic. A KPI is feasible only if every need maps to a column."""
    by_lower = {name.lower(): (name, data_type) for name, data_type in columns.items()}
    resolved = {k.kpi.lower(): k for k in resolution.kpi_fields}
    results: list[Feasibility] = []
    for kpi in contract.kpis:
        needed: list[FeasibilityNeed] = []
        missing: list[FeasibilityMissing] = []
        seen: set[str] = set()
        time_seen = False

        for n in needs.get(kpi.name.lower(), []):
            key = normalize(n.need)
            if key in seen:
                continue
            seen.add(key)
            time_seen = time_seen or n.role == "time"
            found = by_lower.get(n.mapped_to.lower()) if n.mapped_to else None
            if found is None:
                reason = (
                    f"{n.mapped_to} is not a column mapped in the source resolution"
                    if n.mapped_to
                    else "No column in the mapped sources can supply this"
                )
                needed.append(FeasibilityNeed(need=n.need, mapped=None))
                missing.append(FeasibilityMissing(need=n.need, reason=reason))
            elif n.role == "time" and column_kind(found[1]) != "date":
                needed.append(FeasibilityNeed(need=n.need, mapped=None))
                missing.append(
                    FeasibilityMissing(need=n.need, reason=f"{found[0]} is {found[1]}, not a date column")
                )
            else:
                needed.append(FeasibilityNeed(need=n.need, mapped=found[0]))

        # Needs the source resolution already couldn't map count too, so the model can't
        # make a gap disappear by leaving the need out.
        for u in resolved[kpi.name.lower()].unmapped if kpi.name.lower() in resolved else []:
            if normalize(u.need) not in seen:
                seen.add(normalize(u.need))
                time_seen = time_seen or u.role == "time"
                needed.append(FeasibilityNeed(need=u.need, mapped=None))
                missing.append(FeasibilityMissing(need=u.need, reason=u.reason))

        if (kpi.time_logic or "").strip() and not time_seen:
            need = f"date column for time logic: {kpi.time_logic.strip()}"
            needed.append(FeasibilityNeed(need=need, mapped=None))
            missing.append(FeasibilityMissing(need=need, reason="No date column was identified for the time logic"))
        if not needed:
            need = "fields for the KPI's formula intent"
            needed.append(FeasibilityNeed(need=need, mapped=None))
            missing.append(FeasibilityMissing(need=need, reason="No needed fields could be identified"))

        status = FeasibilityStatus.DATA_GAP if missing else FeasibilityStatus.FEASIBLE
        results.append(
            Feasibility(
                kpi=kpi.name,
                status=status,
                needed=needed,
                missing=missing,
                options=list(GAP_OPTIONS) if missing else [],
            )
        )
    return results
