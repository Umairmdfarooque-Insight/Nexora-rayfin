"""The Feasibility artifact (T-402, business rule 8): per KPI, whether the data can support it."""

from pydantic import BaseModel, Field

from app.contracts.business_contract import Feasibility

SCHEMA_VERSION = 1


class FeasibilityReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int
    based_on: dict[str, str] = Field(default_factory=dict)
    kpis: list[Feasibility] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")
