"""Candidate relationships for a run (T-303, FR-007): where joins might exist.

Two sources, both only *candidates* until `relationship_evidence` measures them:
- naming heuristics: `XxxID` in one table matched to `XxxID` or `ID` in a table called `Xxx`;
- the LLM, which sees only table and column names and types plus the contract, never data.

Nothing here touches the database or decides that a relationship is valid.
"""

import json
import re
from dataclasses import dataclass
from typing import Callable

from app.contracts.metadata_inventory import MetadataInventory
from app.discovery.requirement_data_matcher import normalize
from app.llm.errors import LlmInvalidOutput

MAX_CANDIDATES = 200
SCHEMA_NAME = "relationship_candidates"

RelationshipLlm = Callable[[str, str, dict], str]

SYSTEM_PROMPT = """You are Nexora's relationship finder for Microsoft Fabric analytics.
Given a business contract and the tables of a Lakehouse (names and column types only), propose
joins between a column of one table and a column of another table that the contract's entities,
KPIs and dimensions are likely to need.

Rules:
1. Use table names exactly as given ("schema.table") and column names exactly as given.
2. Propose only single-column joins between two different tables.
3. Never invent tables or columns. A proposal is a candidate that code will measure and may reject.
4. Do not generate SQL. Return JSON only."""


def llm_schema() -> dict:
    item = {
        "type": "object",
        "properties": {
            "from_table": {"type": "string"},
            "from_column": {"type": "string"},
            "to_table": {"type": "string"},
            "to_column": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["from_table", "from_column", "to_table", "to_column", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"relationships": {"type": "array", "items": item}},
        "required": ["relationships"],
        "additionalProperties": False,
    }


def default_relationship_llm() -> RelationshipLlm:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


@dataclass(frozen=True)
class Candidate:
    source: str  # "heuristic" | "llm" | "user"
    from_table: str
    from_column: str
    to_table: str
    to_column: str


def table_key(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _aliases(table_name: str) -> set[str]:
    """Normalized names a table can go by: every run of its underscore-separated words
    (SalesLT_Product -> saleslt, product, salesltproduct), plus singular forms."""
    words = [w for w in re.split(r"[_\s]+", table_name) if w]
    found: set[str] = set()
    for i in range(len(words)):
        for j in range(i + 1, len(words) + 1):
            alias = normalize("".join(words[i:j]))
            if alias:
                found.add(alias)
                if alias.endswith("s") and len(alias) > 3:
                    found.add(alias[:-1])
    return found


def heuristic_candidates(inventory: MetadataInventory) -> list[Candidate]:
    """`XxxID` in table A -> `XxxID` (or `ID`) in a table named Xxx. Deterministic and sorted."""
    tables = [(table_key(t.schema_name, t.table_name), t) for t in inventory.tables]
    aliases = {key: _aliases(t.table_name) for key, t in tables}
    found: list[Candidate] = []
    for from_key, source in tables:
        for column in source.columns:
            name = normalize(column.name)
            if not name.endswith("id") or len(name) <= 2:
                continue
            stem = name[:-2]
            for to_key, target in tables:
                if to_key == from_key or stem not in aliases[to_key]:
                    continue
                same = [c for c in target.columns if normalize(c.name) == name]
                plain = [c for c in target.columns if normalize(c.name) == "id"]
                for match in (same or plain)[:1]:
                    found.append(Candidate("heuristic", from_key, column.name, to_key, match.name))
    return sorted(found, key=lambda c: (c.from_table.lower(), c.from_column.lower(), c.to_table.lower()))


def _contract_summary(contract) -> dict:
    if contract is None:
        return {}
    return {
        "business_objective": contract.business_objective,
        "entities": contract.entities,
        "dimensions": contract.dimensions,
        "kpis": [{"name": k.name, "formula_intent": k.formula_intent} for k in contract.kpis],
    }


def llm_candidates(
    contract, inventory: MetadataInventory, llm: RelationshipLlm
) -> list[Candidate]:
    """Relationships the LLM proposes. The payload holds names and types only. The names it
    returns are NOT checked here: the evidence step rejects unknown tables and columns."""
    payload = {
        "contract": _contract_summary(contract),
        "tables": [
            {
                "table": table_key(t.schema_name, t.table_name),
                "columns": [{"name": c.name, "type": c.data_type} for c in t.columns],
            }
            for t in inventory.tables
        ],
    }
    raw = llm(SYSTEM_PROMPT, json.dumps(payload), llm_schema())
    try:
        items = json.loads(raw)["relationships"]
        if not isinstance(items, list):
            raise TypeError("relationships must be a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LlmInvalidOutput(f"The relationship suggestions are unusable: {exc}") from None
    out: list[Candidate] = []
    for item in items:
        try:
            out.append(
                Candidate(
                    "llm",
                    str(item["from_table"]),
                    str(item["from_column"]),
                    str(item["to_table"]),
                    str(item["to_column"]),
                )
            )
        except (KeyError, TypeError):
            continue  # a malformed item is dropped, never guessed at
    return out
