"""The MetadataInventory artifact (spec §10, T-301): tables and columns of a run's scope."""

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class InventoryScope(BaseModel):
    workspace_id: str
    lakehouse_id: str
    schemas: list[str] = Field(default_factory=list)


class InventoryColumn(BaseModel):
    name: str
    data_type: str | None = None
    nullable: bool | None = None
    description: str | None = None


class InventoryError(BaseModel):
    code: str
    message: str


class ColumnProfile(BaseModel):
    null_count: int | None = None
    distinct_count: int | None = None
    is_key_candidate: bool = False
    is_unique: bool | None = None
    duplicate_count: int | None = None  # values that appear more than once (key candidates only)
    min: Any = None  # date and numeric columns only, never strings
    max: Any = None
    approximate: bool = False
    note: str | None = None


class Freshness(BaseModel):
    column: str | None = None
    max_value: str | None = None
    age_days: int | None = None
    status: Literal["ok", "unknown"] = "unknown"


class ProfileEvidence(BaseModel):
    queries: list[str] = Field(default_factory=list)
    profiled_at: str


class TableProfile(BaseModel):
    status: Literal["ok", "warning", "error"]
    code: str | None = None  # set when status is "error"
    message: str | None = None
    row_count: int | None = None
    columns: dict[str, ColumnProfile] = Field(default_factory=dict)
    freshness: Freshness | None = None
    warnings: list[str] = Field(default_factory=list)
    evidence: ProfileEvidence


class ColumnRef(BaseModel):
    table: str  # "schema.table"
    column: str


class RelationshipQueryEvidence(BaseModel):
    queries: list[str] = Field(default_factory=list)
    computed_at: str


class RelationshipEvidence(BaseModel):
    """A candidate join with measured evidence (T-303). Percentages are 0-100."""

    id: str
    from_: ColumnRef = Field(alias="from")
    to: ColumnRef
    source: Literal["heuristic", "llm", "user"]
    type_compatible: bool | None = None
    overlap_pct: float | None = None  # % of distinct non-null source values found in the target
    orphan_rate: float | None = None  # % of non-null source rows with no match in the target
    null_rate: float | None = None  # % of source rows whose key is null
    cardinality: Literal["one_to_one", "many_to_one", "many_to_many"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    thresholds: dict[str, Any] | None = None
    evidence: RelationshipQueryEvidence
    status: Literal["candidate", "rejected"] = "candidate"
    rejected_reason: str | None = None
    notes: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class InventoryTable(BaseModel):
    schema_name: str = Field(alias="schema")
    table_name: str
    description: str | None = None
    columns: list[InventoryColumn] = Field(default_factory=list)
    profile: TableProfile | None = None  # filled by T-302
    errors: list[InventoryError] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class InventoryRunError(BaseModel):
    table: str
    code: str
    message: str


class MetadataInventory(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int
    harvested_at: str
    scope: InventoryScope
    tables: list[InventoryTable] = Field(default_factory=list)
    relationship_evidence: list[RelationshipEvidence] = Field(default_factory=list)  # filled by T-303
    errors: list[InventoryRunError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
