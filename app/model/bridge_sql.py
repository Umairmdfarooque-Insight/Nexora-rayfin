"""Spark SQL for a bridge table derived from an approved rule (T-506, business rule 4).

The rule is free text in config/derivation_rules.yaml. The model drafts one read-only SELECT that
implements it, from the rule text and the names and types of the Lakehouse tables (never data).
Code then refuses anything that isn't a single SELECT over inventory tables, or that hardcodes a
value the rule doesn't state. The accepted SQL goes into the notebook and is saved for review.
"""

import json
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.business_contract import Kpi
from app.contracts.metadata_inventory import MetadataInventory
from app.discovery.derivation_rules import DerivationRule
from app.model.bridge_notebook import TARGET_SCHEMA
from app.model.recon_sql import check_sql

SCHEMA_NAME = "bridge_derivation_sql"
MAX_ATTEMPTS = 2
LlmCall = Callable[[str, str, dict], str]

SYSTEM_PROMPT = """You are Nexora's data engineer for Microsoft Fabric Lakehouses.
Write Spark SQL for ONE SELECT statement that derives a bridge (mapping) table between two business
entities, implementing exactly the approved rule you are given. You see table and column names and
types only, never data.

Rules:
1. The SELECT returns exactly two columns, named exactly as output_columns says (the key of each
   entity's table), one row per related pair.
2. Read-only: one SELECT (or WITH ... SELECT). No INSERT, UPDATE, DELETE, DDL or INTO.
3. Use only the listed tables, as `schema`.`table` with backticks, and only their listed columns.
4. Implement the rule and nothing else. Do not hardcode a number or text unless the rule states
   that exact value. Do not add DISTINCT or other deduplication unless the rule says so.
5. Return JSON only."""


class BridgeSql(BaseModel):
    model_config = ConfigDict(extra="forbid")
    select_sql: str
    explanation: str


class BridgeSqlRejected(Exception):
    def __init__(self, reason: str, attempts: list[dict] | None = None):
        self.reason = reason
        self.attempts = attempts or []
        super().__init__(reason)


def llm_schema() -> dict:
    return {
        "type": "object",
        "properties": {"select_sql": {"type": "string"}, "explanation": {"type": "string"}},
        "required": ["select_sql", "explanation"],
        "additionalProperties": False,
    }


def default_bridge_llm() -> LlmCall:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


def build_user_prompt(
    rule: DerivationRule, entity_tables: dict[str, str], keys: dict[str, str], inventory: MetadataInventory, errors: list[str] | None = None
) -> str:
    payload = {
        "rule": {"id": rule.id, "entities": list(rule.entities), "description": rule.description},
        "entity_tables": entity_tables,  # entity -> "schema.table"
        "output_columns": [keys[e] for e in rule.entities if e in keys],
        "tables": [
            {
                "table": f"{t.schema_name}.{t.table_name}",
                "columns": [{"name": c.name, "data_type": c.data_type} for c in t.columns],
            }
            for t in sorted(inventory.tables, key=lambda t: (t.schema_name.casefold(), t.table_name.casefold()))
            if t.schema_name.casefold() != TARGET_SCHEMA  # tables Nexora generated don't feed another derivation
        ],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if errors:
        text += "\n\nYour previous answer was rejected for these reasons. Fix them and answer again:\n" + "\n".join(f"- {e}" for e in errors)
    return text


def propose_bridge_sql(
    rule: DerivationRule, entity_tables: dict[str, str], keys: dict[str, str], inventory: MetadataInventory, llm: LlmCall
) -> BridgeSql:
    """The accepted SELECT, or `BridgeSqlRejected` after at most 2 attempts."""
    # The rule's text is the only thing it may quote, so check literals against it.
    definition = Kpi(name=rule.id, formula_intent=rule.description)
    errors: list[str] = []
    log: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = llm(SYSTEM_PROMPT, build_user_prompt(rule, entity_tables, keys, inventory, errors), llm_schema())
        try:
            proposal = BridgeSql.model_validate(json.loads(raw))
            errors = check_sql(proposal.select_sql, inventory, definition, "select_sql")
            if '"""' in proposal.select_sql:
                errors.append("select_sql can't contain triple quotes")
        except (json.JSONDecodeError, ValidationError, TypeError):
            proposal, errors = None, ["the answer is not valid JSON in the requested shape"]
        log.append({"attempt": attempt, "raw": raw, "errors": errors})
        if proposal is not None and not errors:
            return proposal
    raise BridgeSqlRejected("; ".join(errors), log)
