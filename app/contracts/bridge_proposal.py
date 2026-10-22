"""The BridgeProposals artifact (T-403, FR-008, business rule 4).

A proposal is a design only: nothing is generated here. `needs_rules` means no mapping table
and no approved derivation rule exists, so the mapping must come from a person.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Situation = Literal["existing_table", "derivation_rule", "needs_rules"]
BridgeStatus = Literal["proposed", "approved", "rejected"]


class BridgeProposal(BaseModel):
    id: str
    entities: list[str]
    situation: Situation
    table: str | None = None  # "schema.table" of the existing mapping table
    derivation_rule: str | None = None  # id in config/derivation_rules.yaml
    deduplication_rule: str | None = None  # approved rule that allows duplicate pairs
    evidence: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    status: BridgeStatus = "proposed"


class BridgeProposals(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int
    based_on: dict[str, str] = Field(default_factory=dict)
    proposals: list[BridgeProposal] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")
