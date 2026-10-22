"""Entity-to-source mapping (T-401, FR-009, business rule 5).

For each contract entity, candidate tables come from the glossary (deterministic) and from the
model's suggestions. Every candidate is scored, and the best eligible one is selected. The
model only proposes: a table or column it names that is not in the inventory is discarded, and
the scoring, the conformed-table policy and the selection are code. The model sees names and
types only, never data.

Score (0 to 1): 0.4*name_match + 0.3*column_coverage + 0.2*conformed + 0.1*relationship_confidence
- name_match: 1.0 if a glossary word is the table's name (a schema-wide prefix such as `SalesLT_`
  and one leading qualifier such as `dim_` ignored), 0.7 if it is a run of the words in the name, else 0 (a model-only suggestion).
- column_coverage: of the KPI needs that have a candidate column in one of the entity's candidate
  tables, the share this table can satisfy (1.0 when no need points at the entity).
- conformed: 1 if the table is an approved conformed source, else 0.
- relationship_confidence: the best measured confidence (high 1, medium 0.6, low 0.3) of a
  relationship between this table and another candidate table.
Ties: higher score, then conformed first, then table name. A table that is not conformed can be
selected only with an approved exception.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.source_resolution import (
    EntityResolution,
    FieldMapping,
    KpiFields,
    RejectedSource,
    SelectedSource,
    UnmappedNeed,
)
from app.discovery.glossary import Glossary, forms, normalize
from app.llm.errors import LlmInvalidOutput

logger = logging.getLogger(__name__)

SCHEMA_NAME = "source_suggestions"
ROLES = ("measure", "filter", "time", "dimension")
CONFIDENCE_VALUE = {"high": 1.0, "medium": 0.6, "low": 0.3}
WEIGHTS = {"name": 0.4, "coverage": 0.3, "conformed": 0.2, "relationship": 0.1}

SourceLlm = Callable[[str, str, dict], str]

SYSTEM_PROMPT = """You are Nexora's source mapper for Microsoft Fabric analytics.
Given a business contract, a glossary and the tables of a Lakehouse (names and column types
only), say which tables hold each business entity and which columns each KPI needs.

Rules:
1. Use table names exactly as given ("schema.table") and column names exactly as given.
2. For each entity, list the tables that could be its source, best first. Only tables from the list.
3. For each KPI, list what its formula intent, filters, time logic and exclusions need, as short
   phrases ("line amount", "order status", "order date"), each with a role (measure, filter, time,
   dimension) and the columns that could supply it. If no column could, give an empty list.
4. Never invent tables or columns. Your answer is a candidate that code will check and may discard.
5. Do not generate SQL or DAX. Return JSON only."""


def llm_schema() -> dict:
    candidate = {
        "type": "object",
        "properties": {"table": {"type": "string"}, "column": {"type": "string"}},
        "required": ["table", "column"],
        "additionalProperties": False,
    }
    need = {
        "type": "object",
        "properties": {
            "need": {"type": "string"},
            "role": {"type": "string", "enum": list(ROLES)},
            "candidates": {"type": "array", "items": candidate},
        },
        "required": ["need", "role", "candidates"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "tables": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["entity", "tables"],
                    "additionalProperties": False,
                },
            },
            "kpi_fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"kpi": {"type": "string"}, "fields": {"type": "array", "items": need}},
                    "required": ["kpi", "fields"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["entities", "kpi_fields"],
        "additionalProperties": False,
    }


def default_source_llm() -> SourceLlm:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


def table_key(table) -> str:
    return f"{table.schema_name}.{table.table_name}"


def build_payload(contract, inventory: MetadataInventory, glossary_terms: dict[str, list[str]]) -> dict:
    """What the model sees: the contract, the glossary, table and column names and types."""
    return {
        "contract": {
            "business_objective": contract.business_objective,
            "entities": contract.entities,
            "dimensions": contract.dimensions,
            "filters": contract.filters,
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
        },
        "glossary": glossary_terms,
        "tables": [
            {"table": table_key(t), "columns": [{"name": c.name, "type": c.data_type} for c in t.columns]}
            for t in inventory.tables
        ],
    }


@dataclass
class Need:
    need: str
    role: str
    candidates: list[tuple[str, str]]  # valid (table, column) pairs, canonical names


@dataclass
class Suggestions:
    entity_tables: dict[str, list[str]] = field(default_factory=dict)  # normalized entity -> tables
    needs: dict[str, list[Need]] = field(default_factory=dict)  # KPI name (lower case) -> needs


def parse_suggestions(raw: str, contract, inventory: MetadataInventory) -> Suggestions:
    """Parse the model's answer, keeping only what exists in the inventory. Anything the model
    names that isn't there is discarded and logged, never selected."""
    try:
        data = json.loads(raw)
        entities, kpis = data["entities"], data["kpi_fields"]
        if not isinstance(entities, list) or not isinstance(kpis, list):
            raise TypeError("entities and kpi_fields must be lists")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LlmInvalidOutput(f"The source suggestions are unusable: {exc}") from None

    columns = {
        table_key(t).lower(): (table_key(t), {c.name.lower(): c.name for c in t.columns})
        for t in inventory.tables
    }
    wanted_entities = {normalize(e) for e in contract.entities}
    wanted_kpis = {k.name.lower() for k in contract.kpis}
    out = Suggestions()

    for item in entities:
        try:
            entity, tables = normalize(str(item["entity"])), item["tables"]
            if entity not in wanted_entities or not isinstance(tables, list):
                continue
        except (KeyError, TypeError):
            continue
        for name in tables:
            found = columns.get(str(name).strip().lower())
            if found is None:
                logger.warning("Discarded a suggested table that is not in the inventory: %r", name)
            elif found[0] not in out.entity_tables.setdefault(entity, []):
                out.entity_tables[entity].append(found[0])

    for item in kpis:
        try:
            kpi, fields = str(item["kpi"]).strip().lower(), item["fields"]
            if kpi not in wanted_kpis or not isinstance(fields, list):
                continue
        except (KeyError, TypeError):
            continue
        seen = {n.need.lower() for n in out.needs.get(kpi, [])}
        for f in fields:
            try:
                need, role, cands = str(f["need"]).strip(), str(f["role"]), f["candidates"]
                if not need or role not in ROLES or need.lower() in seen or not isinstance(cands, list):
                    continue
                valid: list[tuple[str, str]] = []
                for c in cands:
                    found = columns.get(str(c["table"]).strip().lower())
                    column = found[1].get(str(c["column"]).strip().lower()) if found else None
                    if column is None:
                        logger.warning("Discarded a suggested column that is not in the inventory: %r", c)
                    elif (found[0], column) not in valid:
                        valid.append((found[0], column))
            except (KeyError, TypeError):
                continue
            seen.add(need.lower())
            out.needs.setdefault(kpi, []).append(Need(need, role, valid))
    return out


# --- name matching -------------------------------------------------------------------------

_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _parts(name: str) -> list[str]:
    return [p for p in re.split(r"[_\s]+", name) if p]


def schema_prefixes(inventory: MetadataInventory) -> dict[str, str]:
    """Per schema, a first name part shared by at least half of its tables (and two tables),
    such as `SalesLT_`. It says nothing about what a table holds, so name matching skips it."""
    prefixes: dict[str, str] = {}
    by_schema: dict[str, list[list[str]]] = {}
    for t in inventory.tables:
        by_schema.setdefault(t.schema_name.lower(), []).append(_parts(t.table_name))
    for schema, tables in by_schema.items():
        firsts = Counter(p[0].lower() for p in tables if len(p) > 1)
        if firsts:
            first, count = sorted(firsts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if count >= 2 and count * 2 >= len(tables):
                prefixes[schema] = first
    return prefixes


def name_match(aliases: list[str], table, prefixes: dict[str, str]) -> tuple[float, str | None]:
    """(0, 0.7 or 1.0) and the glossary word that matched."""
    parts = _parts(table.table_name)
    if len(parts) > 1 and parts[0].lower() == prefixes.get(table.schema_name.lower()):
        parts = parts[1:]
    # The whole name, or the name without one leading qualifier (`dim_`, `SalesLT_`). Dropping
    # more would let `bridge_product_promotion` count as being named `promotion`.
    exact = {f for i in range(min(2, len(parts))) for f in forms(normalize("".join(parts[i:])))}
    words = [w.lower() for p in parts for w in (_WORDS.findall(p) or [p])]
    runs = {
        f
        for i in range(len(words))
        for j in range(i + 1, len(words) + 1)
        for f in forms(normalize("".join(words[i:j])))
    }
    best, matched = 0.0, None
    for alias in aliases:
        for f in forms(alias):
            score = 1.0 if f in exact else 0.7 if f in runs else 0.0
            if score > best:
                best, matched = score, alias
    return best, matched


# --- resolution ----------------------------------------------------------------------------


def _key_note(table) -> str | None:
    """Key evidence from the table's profile, if it has one (it never changes the score)."""
    if table.profile is None:
        return None
    keys = [(n, p) for n, p in table.profile.columns.items() if p.is_key_candidate and p.is_unique is not None]
    for name, profile in keys:
        if profile.is_unique:
            return f"key {name} unique"
    if keys:
        return f"key {keys[0][0]} not unique"
    return None


def _relationship_scores(inventory: MetadataInventory, candidates: set[str]) -> dict[str, float]:
    best: dict[str, float] = {}
    for e in inventory.relationship_evidence:
        if e.status != "candidate" or e.confidence is None:
            continue
        a, b = e.from_.table.lower(), e.to.table.lower()
        value = CONFIDENCE_VALUE[e.confidence]
        for mine, other in ((a, b), (b, a)):
            if other in candidates and other != mine:
                best[mine] = max(best.get(mine, 0.0), value)
    return best


def resolve(
    contract,
    inventory: MetadataInventory,
    glossary: Glossary,
    conformed: frozenset[str],
    exceptions: dict[str, str],
    suggestions: Suggestions,
) -> tuple[list[EntityResolution], list[KpiFields]]:
    """Pure and deterministic: the same inputs always give the same resolution.

    `conformed` holds lower-cased "schema.table" names; `exceptions` maps a lower-cased table
    to the approval id of its exception."""
    tables = {table_key(t): t for t in inventory.tables}
    prefixes = schema_prefixes(inventory)
    entities: list[tuple[str, str]] = []  # (entity, normalized)
    for entity in contract.entities:
        if normalize(entity) and normalize(entity) not in {n for _, n in entities}:
            entities.append((entity, normalize(entity)))

    # 1. candidate tables per entity, with their name match
    candidates: dict[str, dict[str, tuple[float, str | None]]] = {}
    for entity, norm in entities:
        aliases = glossary.aliases_for(entity)
        found: dict[str, tuple[float, str | None]] = {}
        for key, table in tables.items():
            score, word = name_match(aliases, table, prefixes)
            if score > 0:
                found[key] = (score, word)
        for key in suggestions.entity_tables.get(norm, []):
            found.setdefault(key, (0.0, None))
        candidates[norm] = found
    every_candidate = {k.lower() for found in candidates.values() for k in found}
    relationship = _relationship_scores(inventory, every_candidate)

    # 2. score, select, reject
    all_needs = [n for needs in suggestions.needs.values() for n in needs]
    resolved: list[EntityResolution] = []
    selected_tables: set[str] = set()
    for entity, norm in entities:
        found = candidates[norm]
        if not found:
            resolved.append(
                EntityResolution(
                    entity=entity,
                    reason=f"No table in the inventory matches {entity!r} (glossary or model suggestions)",
                )
            )
            continue
        mine = {k.lower() for k in found}
        attributed = [n for n in all_needs if any(t.lower() in mine for t, _ in n.candidates)]
        rows = []
        for key, (match, word) in found.items():
            lower = key.lower()
            coverage = (
                sum(1 for n in attributed if any(t == key for t, _ in n.candidates)) / len(attributed)
                if attributed
                else 1.0
            )
            is_conformed = lower in conformed
            score = round(
                WEIGHTS["name"] * match
                + WEIGHTS["coverage"] * coverage
                + WEIGHTS["conformed"] * (1.0 if is_conformed else 0.0)
                + WEIGHTS["relationship"] * relationship.get(lower, 0.0),
                2,
            )
            eligible = is_conformed or lower in exceptions
            rows.append((key, score, is_conformed, eligible, word, coverage, len(attributed)))
        rows.sort(key=lambda r: (-r[1], not r[2], r[0].lower()))

        chosen = next((r for r in rows if r[3]), None)
        rejected: list[RejectedSource] = []
        for key, score, _, eligible, *_ in rows:
            if chosen is not None and key == chosen[0]:
                continue
            note = _key_note(tables[key])
            if not eligible:
                reason = "Not conformed and no approved exception" + (f"; {note}" if note else "")
            elif score == chosen[1]:
                reason = f"Tied with {chosen[0]} at {score}; lost the tie-break (conformed first, then table name)"
            else:
                reason = f"Scored {score}, below {chosen[0]} at {chosen[1]}"
            rejected.append(RejectedSource(table=key, score=score, reason=reason, requires_exception=not eligible))

        if chosen is None:
            resolved.append(
                EntityResolution(
                    entity=entity,
                    reason=(
                        "No candidate is conformed or has an approved exception; "
                        "a data_engineer must approve an exception for one of them"
                    ),
                    rejected=rejected,
                )
            )
            continue
        key, score, is_conformed, _, word, coverage, needed = chosen
        selected_tables.add(key)
        parts = [f"Glossary match {word!r}" if word else "Suggested by the model; no glossary match"]
        parts.append("conformed" if is_conformed else f"approved exception {exceptions[key.lower()]}")
        note = _key_note(tables[key])
        if note:
            parts.append(note)
        if needed:
            parts.append(f"covers {round(coverage * needed)} of {needed} needed fields")
        resolved.append(
            EntityResolution(
                entity=entity,
                selected=SelectedSource(
                    table=key,
                    score=score,
                    conformed=is_conformed,
                    exception_id=None if is_conformed else exceptions[key.lower()],
                    rationale="; ".join(parts),
                ),
                rejected=rejected,
            )
        )

    # 3. map each KPI's needs to one column
    def usable(table: str) -> bool:
        return table.lower() in conformed or table.lower() in exceptions

    kpi_fields: list[KpiFields] = []
    for kpi in contract.kpis:
        mapped: list[FieldMapping] = []
        unmapped: list[UnmappedNeed] = []
        for need in suggestions.needs.get(kpi.name.lower(), []):
            options = sorted(
                (c for c in need.candidates if usable(c[0])),
                key=lambda c: (c[0] not in selected_tables, c[0].lower(), c[1].lower()),
            )
            if options:
                mapped.append(FieldMapping(need=need.need, table=options[0][0], column=options[0][1], role=need.role))
            else:
                reason = (
                    "Its candidate columns are in tables that are not conformed and have no approved exception"
                    if need.candidates
                    else "No column in the inventory matches this need"
                )
                unmapped.append(UnmappedNeed(need=need.need, role=need.role, reason=reason))
        kpi_fields.append(KpiFields(kpi=kpi.name, fields=mapped, unmapped=unmapped))
    return resolved, kpi_fields
