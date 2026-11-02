"""The ColumnDefinitions artifact: optional, user-supplied column descriptions uploaded on
the New Run form (table, column, definition), merged into MetadataInventory at harvest time
to help the model understand ambiguous columns during source resolution and feasibility."""

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class ColumnDefinitionEntry(BaseModel):
    schema_name: str = Field(alias="schema")
    table_name: str = Field(alias="table")
    column_name: str = Field(alias="column")
    definition: str

    model_config = {"populate_by_name": True}


class ColumnDefinitions(BaseModel):
    schema_version: int
    run_id: str
    version: int
    uploaded_at: str
    filename: str
    entries: list[ColumnDefinitionEntry] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")
