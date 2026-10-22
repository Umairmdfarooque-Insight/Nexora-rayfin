"""ModelSpec.yaml and ExecutionPlan.yaml (T-404, FR-010, spec §10).

Both are generated deterministically from approved inputs and reviewed before any model is
generated. `extra="forbid"` means a typo in a hand-edited file is an error, not a silent no-op.
Files are saved with a fixed key order, UTF-8 and LF line endings.
"""

from pathlib import Path
from typing import Literal, TypeVar

import yaml

from app.fsutil import replace_file
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

StorageMode = Literal["DirectLake", "Import"]
TableRole = Literal["fact", "dimension", "bridge", "date"]
Cardinality = Literal["many_to_one", "one_to_one", "many_to_many"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- ModelSpec -------------------------------------------------------------------------------


class ModelColumn(_Strict):
    name: str
    source_column: str
    data_type: str
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False


class HierarchyLevel(_Strict):
    name: str
    column: str


class Hierarchy(_Strict):
    name: str
    levels: list[HierarchyLevel]


class TableSource(_Strict):
    schema_name: str = Field(alias="schema")
    table: str


class ModelTable(_Strict):
    name: str
    source: TableSource | None = None  # None would be a calculated table, which Direct Lake can't use
    role: TableRole
    grain: str | None = None
    columns: list[ModelColumn] = Field(default_factory=list)
    hierarchies: list[Hierarchy] = Field(default_factory=list)


class RelationshipEnd(_Strict):
    table: str
    column: str


class Relationship(_Strict):
    name: str
    from_: RelationshipEnd = Field(alias="from")  # the many side
    to: RelationshipEnd
    cardinality: Cardinality
    cross_filter: Literal["single", "both"] = "single"
    evidence_ref: str | None = None  # id in MetadataInventory.relationship_evidence
    override_rationale: str | None = None  # explicit override of missing or weak evidence (rule 7)


class Measure(_Strict):
    name: str
    table: str
    kpi: str
    formula_intent: str | None = None
    dax: str | None = None  # always null until T-503
    format_string: str | None = None
    display_folder: str | None = None
    description: str | None = None


class RlsRole(_Strict):
    name: str
    description: str
    applies_to: list[str] = Field(default_factory=list)
    table_filters: list[dict[str, str]] = Field(default_factory=list)


class ModelSpec(_Strict):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int = Field(default=1, ge=1)
    based_on: dict[str, str] = Field(default_factory=dict)
    model_name: str
    storage_mode: StorageMode = "DirectLake"
    tables: list[ModelTable] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    roles: list[RlsRole] = Field(default_factory=list)


# --- ExecutionPlan ---------------------------------------------------------------------------


class PlanSource(_Strict):
    table: str  # "schema.table"
    role: TableRole
    conformed: bool
    exception_id: str | None = None


class Stage(_Strict):
    name: str
    gate: str
    depends_on: list[str] = Field(default_factory=list)


class Target(_Strict):
    workspace_id: str | None = None
    kind: Literal["sandbox"] = "sandbox"


class RequiredApproval(_Strict):
    gate: str
    roles: list[str]


class DataEngineeringAction(_Strict):
    id: str
    kind: Literal["create_date_table", "create_bridge_table"]
    description: str
    blocking: bool = True
    owner: str | None = None
    status: Literal["open", "done"] = "open"


class ExecutionPlan(_Strict):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    version: int = Field(default=1, ge=1)
    based_on: dict[str, str] = Field(default_factory=dict)
    sources: list[PlanSource] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    target: Target = Field(default_factory=Target)
    approvals_required: list[RequiredApproval] = Field(default_factory=list)
    bridges: list[str] = Field(default_factory=list)
    data_engineering_actions: list[DataEngineeringAction] = Field(default_factory=list)
    feasibility_ref: str = "Feasibility.json"


# --- files -----------------------------------------------------------------------------------

M = TypeVar("M", ModelSpec, ExecutionPlan)


def to_data(model: BaseModel) -> dict:
    return model.model_dump(mode="json", by_alias=True)


def dump_yaml(model: BaseModel) -> str:
    return yaml.safe_dump(to_data(model), sort_keys=False, allow_unicode=True, width=120)


def save_yaml(model: BaseModel, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(dump_yaml(model).encode("utf-8"))  # bytes: Windows keeps LF
    replace_file(tmp, path)


def load_yaml(cls: type[M], path: Path) -> M:
    return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
