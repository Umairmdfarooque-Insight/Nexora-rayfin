"""How an approved bridge becomes model objects (T-506, FR-008, business rule 4).

A bridge table (an existing mapping table, its deduplicated copy, or a table derived from an
approved rule) joins two entity tables through two many-to-one relationships, each from the bridge
to the entity table: one-to-many from each dimension to the bridge. Filters must reach the facts
from either side, so the relationship to the entity table that is connected to a fact table is
bi-directional and the other one is single: a filter on the other entity flows
other -> bridge -> anchor -> fact.
"""

from dataclasses import dataclass

from app.contracts.bridge_proposal import BridgeProposal
from app.contracts.metadata_inventory import InventoryTable, MetadataInventory
from app.contracts.model_plan import (
    ModelColumn,
    ModelSpec,
    ModelTable,
    Relationship,
    RelationshipEnd,
    TableSource,
)
from app.contracts.source_resolution import SourceResolution
from app.discovery.glossary import normalize
from app.discovery.model_planner import model_type
from app.discovery.relationship_candidates import table_key

STRONG = ("high", "medium")


class BridgeError(Exception):
    """The bridge can't be placed in the model; the message says why."""


@dataclass(frozen=True)
class Side:
    entity: str
    model_table: str  # the entity's table in the ModelSpec
    model_column: str  # its key column
    source_key: str  # "schema.table" of the entity's source
    key: str  # the key column in the source
    bridge_column: str | None  # the mapping table's column that joins to it (existing tables only)
    evidence_id: str | None


def entity_key_column(table: InventoryTable) -> str:
    """The column that identifies a row of an entity table: a unique key candidate from its profile,
    else the first column whose name ends in ID."""
    profile = table.profile
    if profile is not None:
        for c in table.columns:
            p = profile.columns.get(c.name)
            if p is not None and p.is_key_candidate and p.is_unique:
                return c.name
    for c in table.columns:
        if c.name.lower().endswith("id"):
            return c.name
    raise BridgeError(f"No key column found in {table_key(table.schema_name, table.table_name)}")


def find_sides(
    proposal: BridgeProposal,
    resolution: SourceResolution,
    inventory: MetadataInventory,
    spec: ModelSpec,
    mapping_table: str | None,
) -> list[Side]:
    """The two entity sides of a proposal, in the proposal's order."""
    by_key = {table_key(t.schema_name, t.table_name).lower(): t for t in inventory.tables}
    strong = [e for e in inventory.relationship_evidence if e.status == "candidate" and e.confidence in STRONG]
    sides = []
    for entity in proposal.entities:
        found = next((r for r in resolution.entities if normalize(r.entity) == normalize(entity) and r.selected), None)
        if found is None:
            raise BridgeError(f"No table was selected for the entity {entity!r}")
        source = found.selected.table
        inv = by_key.get(source.lower())
        model = next((t for t in spec.tables if t.source and f"{t.source.schema_name}.{t.source.table}".lower() == source.lower()), None)
        if inv is None or model is None:
            raise BridgeError(f"The table of entity {entity!r} ({source}) is not in the model")
        bridge_column = evidence_id = None
        if mapping_table:
            options = sorted(
                (e for e in strong if e.from_.table.lower() == mapping_table.lower() and e.to.table.lower() == source.lower()),
                key=lambda e: (e.to.column.lower(), e.from_.column.lower()),
            )
            if not options:
                raise BridgeError(f"No relationship evidence joins {mapping_table} to {source}")
            bridge_column, key, evidence_id = options[0].from_.column, options[0].to.column, options[0].id
        else:
            key = entity_key_column(inv)
        column = next((c for c in model.columns if c.source_column.lower() == key.lower()), None)
        if column is None:
            raise BridgeError(f"The key {key} of {source} is not a column of the model table {model.name}")
        sides.append(Side(entity, model.name, column.name, source, key, bridge_column, evidence_id))
    if len({s.model_table for s in sides}) != 2:
        raise BridgeError("A bridge needs two different entity tables")
    return sides


def _anchor(spec: ModelSpec, sides: list[Side], bridge: str) -> str | None:
    """The side whose table is related to a fact table (the first, if both are)."""
    facts = {t.name for t in spec.tables if t.role == "fact"}
    for side in sides:
        for r in spec.relationships:
            if r.from_.table != bridge and r.to.table != bridge and {r.from_.table, r.to.table} & facts and side.model_table in (r.from_.table, r.to.table):
                return side.model_table
    return None


def apply_bridge(
    spec: ModelSpec,
    sides: list[Side],
    source: InventoryTable,
    bridge_columns: tuple[str, str],
    rationale: str | None = None,
) -> ModelSpec:
    """A copy of `spec` with the bridge table and its two relationships (or their directions, if the
    plan already had them). `bridge_columns` are the bridge's columns that join to each side."""
    spec = spec.model_copy(deep=True)
    key = f"{source.schema_name}.{source.table_name}".lower()
    bridge = next((t for t in spec.tables if t.source and f"{t.source.schema_name}.{t.source.table}".lower() == key), None)
    if bridge is None:
        names = {t.name.casefold() for t in spec.tables}
        name = source.table_name if source.table_name.casefold() not in names else f"{source.table_name}_bridge"
        hidden = {c.lower() for c in bridge_columns}
        columns = [
            ModelColumn(name=c.name, source_column=c.name, data_type=mtype, is_hidden=c.name.lower() in hidden)
            for c in source.columns
            if (mtype := model_type(c.data_type)) is not None
        ]
        bridge = ModelTable(name=name, source=TableSource(schema=source.schema_name, table=source.table_name), role="bridge", columns=columns)
        spec.tables.append(bridge)
    else:
        bridge.role = "bridge"
        for c in bridge.columns:
            c.is_hidden = c.is_hidden or c.source_column.lower() in {x.lower() for x in bridge_columns}

    anchor = _anchor(spec, sides, bridge.name)
    for side, column in zip(sides, bridge_columns):
        model_column = next((c.name for c in bridge.columns if c.source_column.lower() == column.lower()), None)
        if model_column is None:
            raise BridgeError(f"The bridge has no column {column}")
        cross = "both" if side.model_table == anchor else "single"
        existing = next(
            (r for r in spec.relationships if {r.from_.table, r.to.table} == {bridge.name, side.model_table}), None
        )
        if existing is not None:
            existing.cross_filter = cross
            continue
        spec.relationships.append(
            Relationship(
                name=f"{bridge.name}_{side.model_table}",
                from_=RelationshipEnd(table=bridge.name, column=model_column),
                to=RelationshipEnd(table=side.model_table, column=side.model_column),
                cardinality="many_to_one",
                cross_filter=cross,
                evidence_ref=side.evidence_id,
                override_rationale=None if side.evidence_id else rationale,
            )
        )
    return spec
