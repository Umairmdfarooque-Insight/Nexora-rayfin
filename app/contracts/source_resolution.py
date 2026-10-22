"""The SourceResolution artifact (T-401, FR-009): which table serves each entity and which
columns serve each KPI's needs, with the rationale for every selected and rejected source."""

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Role = Literal["measure", "filter", "time", "dimension"]


class SelectedSource(BaseModel):
    table: str  # "schema.table"
    score: float
    conformed: bool
    exception_id: str | None = None  # approval id of the exception that allows a non-conformed table
    rationale: str


class RejectedSource(BaseModel):
    table: str
    score: float
    reason: str
    requires_exception: bool = False  # not conformed and no approved exception (business rule 5)


class EntityResolution(BaseModel):
    entity: str
    selected: SelectedSource | None = None
    reason: str | None = None  # why nothing was selected
    rejected: list[RejectedSource] = Field(default_factory=list)


class FieldMapping(BaseModel):
    need: str
    table: str
    column: str
    role: Role


class UnmappedNeed(BaseModel):
    need: str
    role: Role
    reason: str


class KpiFields(BaseModel):
    kpi: str
    fields: list[FieldMapping] = Field(default_factory=list)
    unmapped: list[UnmappedNeed] = Field(default_factory=list)


class SourceResolution(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int
    based_on: dict[str, str] = Field(default_factory=dict)
    entities: list[EntityResolution] = Field(default_factory=list)
    kpi_fields: list[KpiFields] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")
