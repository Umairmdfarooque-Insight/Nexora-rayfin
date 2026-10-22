"""ModelSpec and ExecutionPlan generation (T-404, FR-010).

Deterministic: the same contract, inventory, source resolution, feasibility and bridge proposals
always give the same two files. There is no LLM here. Tables come from the sources selected for
the contract's entities and from the columns the KPIs need; relationships come only from
measured evidence (medium or high confidence); only feasible KPIs become measures (formula
intent only, `dax: null`); RLS roles come only from the contract's security section. Direct
Lake is the default, so no calculated tables are ever created: a missing date table becomes a
data-engineering action instead.
"""

import re
from dataclasses import dataclass

from app.contracts.bridge_proposal import BridgeProposals
from app.contracts.business_contract import BusinessContract, FeasibilityStatus
from app.contracts.feasibility_report import FeasibilityReport
from app.contracts.metadata_inventory import InventoryTable, MetadataInventory
from app.contracts.model_plan import (
    DataEngineeringAction,
    ExecutionPlan,
    Hierarchy,
    HierarchyLevel,
    Measure,
    ModelColumn,
    ModelSpec,
    ModelTable,
    PlanSource,
    RequiredApproval,
    Relationship,
    RelationshipEnd,
    RlsRole,
    Stage,
    Target,
    TableSource,
)
from app.contracts.source_resolution import SourceResolution
from app.discovery.glossary import normalize
from app.discovery.relationship_evidence import type_family

STRONG = ("high", "medium")
MODEL_TYPES = {"integer": "int64", "decimal": "decimal", "string": "string", "date": "dateTime", "boolean": "boolean"}
CURRENCY_FORMATS = {"USD": "$#,0", "$": "$#,0", "EUR": "€#,0", "GBP": "£#,0"}
DEFAULT_MEASURE_FORMAT = "#,0"
DECIMAL_FORMAT = "#,0.00"
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class NoFeasibleKpis(Exception):
    def __init__(self):
        super().__init__("No feasible KPIs: every KPI is a data gap, so there is nothing to plan")


@dataclass
class PlanInputs:
    run_id: str
    contract: BusinessContract
    inventory: MetadataInventory
    resolution: SourceResolution
    feasibility: FeasibilityReport
    bridges: BridgeProposals | None
    conformed: frozenset[str]
    exceptions: dict[str, str]
    based_on: dict[str, str]
    workspace_id: str | None


def key_of(table: InventoryTable) -> str:
    return f"{table.schema_name}.{table.table_name}"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_ \-]", "_", name).strip() or "Table"


def model_type(data_type: str | None) -> str | None:
    return MODEL_TYPES.get(type_family(data_type))


def _split(mapped: str) -> tuple[str, str]:
    table, _, column = mapped.rpartition(".")
    return table, column


def _date_table(inventory: MetadataInventory, eligible: set[str], taken: set[str]) -> InventoryTable | None:
    """A conformed (or excepted) table named for dates with a date column, best first."""
    found = []
    for t in inventory.tables:
        key = key_of(t).lower()
        words = {w.lower() for p in re.split(r"[_\s]+", t.table_name) for w in (_WORDS.findall(p) or [p])}
        dates = [c for c in t.columns if model_type(c.data_type) == "dateTime"]
        if key in eligible and key not in taken and words & {"date", "calendar"} and dates:
            found.append((0 if any(c.name.lower() == "date" for c in dates) else 1, key, t))
    return min(found, key=lambda f: f[:2])[2] if found else None


def _hierarchy(table: InventoryTable) -> list[Hierarchy]:
    """A Calendar hierarchy from the date table's Year, Quarter and Month columns, if it has two."""
    names = {normalize(c.name): c.name for c in table.columns if model_type(c.data_type) is not None}
    levels = []
    for label, options in (("Year", ("year",)), ("Quarter", ("quarter",)), ("Month", ("monthname", "month"))):
        key = next((o for o in options if o in names), None)
        if key:
            levels.append(HierarchyLevel(name=label, column=names[key]))
    return [Hierarchy(name="Calendar", levels=levels)] if len(levels) >= 2 else []


def build_plan(inp: PlanInputs) -> tuple[ModelSpec, ExecutionPlan]:
    """The model spec and the execution plan (version 1; the stage sets versions and hashes)."""
    inventory = inp.inventory
    by_key = {key_of(t).lower(): t for t in inventory.tables}
    feasible = {k.kpi.lower(): k for k in inp.feasibility.kpis if k.status is FeasibilityStatus.FEASIBLE}
    kpis = [k for k in inp.contract.kpis if k.name.lower() in feasible]
    if not kpis:
        raise NoFeasibleKpis()

    # Roles of mapped fields, from the source resolution.
    role_of = {
        (f.table.lower(), f.column.lower()): f.role for kf in inp.resolution.kpi_fields for f in kf.fields
    }
    fields: dict[str, list[tuple[str, str, str]]] = {}  # kpi -> (table key, column, role)
    for kpi in kpis:
        for need in feasible[kpi.name.lower()].needed:
            if need.mapped:
                table, column = _split(need.mapped)
                if table.lower() in by_key:
                    t = by_key[table.lower()]
                    col = next((c.name for c in t.columns if c.name.lower() == column.lower()), column)
                    fields.setdefault(kpi.name, []).append((key_of(t), col, role_of.get((table.lower(), column.lower()), "dimension")))

    def measure_table(kpi_name: str) -> str:
        mine = fields.get(kpi_name, [])
        return next((t for t, _, r in mine if r == "measure"), mine[0][0] if mine else "")

    facts: list[str] = []
    for kpi in kpis:
        t = measure_table(kpi.name)
        if t and t not in facts:
            facts.append(t)

    # Tables, in a stable order: facts, entity tables, other KPI tables, bridges, date.
    ordered: list[str] = []

    def add(key: str) -> None:
        if key and key.lower() not in {o.lower() for o in ordered}:
            ordered.append(key_of(by_key[key.lower()]))

    for t in facts:
        add(t)
    entity_name: dict[str, str] = {}
    for e in inp.resolution.entities:
        if e.selected and e.selected.table.lower() in by_key:
            entity_name.setdefault(e.selected.table.lower(), e.entity)
            add(e.selected.table)
    for kpi in kpis:
        for t, _, _ in fields.get(kpi.name, []):
            add(t)

    proposals = [p for p in (inp.bridges.proposals if inp.bridges else []) if p.status != "rejected"]
    bridge_tables = {p.table.lower() for p in proposals if p.table and p.table.lower() in by_key}
    for p in proposals:
        if p.table and p.table.lower() in by_key:
            add(p.table)

    time_needed = any((k.time_logic or "").strip() for k in kpis) or any(
        r == "time" for k in kpis for _, _, r in fields.get(k.name, [])
    )
    eligible = {c for c in by_key if c in inp.conformed or c in inp.exceptions}
    actions: list[DataEngineeringAction] = []
    date = _date_table(inventory, eligible, {o.lower() for o in ordered}) if time_needed else None
    if date is not None:
        add(key_of(date))
    elif time_needed:
        actions.append(
            DataEngineeringAction(
                id="DE1",
                kind="create_date_table",
                description=(
                    "No conformed date table is in scope. Create a physical date table in the Lakehouse "
                    "(Direct Lake can't use a calculated table) and add it to the conformed sources."
                ),
            )
        )

    # Names, unique within the model.
    used: set[str] = set()

    def unique(name: str) -> str:
        base, n, candidate = _safe(name), 1, _safe(name)
        while candidate.lower() in used:
            n += 1
            candidate = f"{base}_{n}"
        used.add(candidate.lower())
        return candidate

    names: dict[str, str] = {}
    for key in ordered:
        low = key.lower()
        if date is not None and low == key_of(date).lower():
            names[low] = unique("Date")
        else:
            names[low] = unique(entity_name.get(low) or by_key[low].table_name)

    # Relationships: measured evidence only, between tables in the model.
    column_ok = {
        low: {c.name.lower() for c in by_key[low].columns if model_type(c.data_type) is not None} for low in names
    }

    def evidence_number(e) -> int:
        return int(e.id[1:]) if e.id[1:].isdigit() else 10**9

    relationships: list[Relationship] = []
    seen_pairs: set[frozenset] = set()
    for e in sorted(inventory.relationship_evidence, key=evidence_number):
        a, b = e.from_.table.lower(), e.to.table.lower()
        if (
            e.status != "candidate"
            or e.confidence not in STRONG
            or e.cardinality not in ("many_to_one", "one_to_one")
            or a not in names
            or b not in names
            or a == b
            or e.from_.column.lower() not in column_ok[a]
            or e.to.column.lower() not in column_ok[b]
        ):
            continue
        identity = frozenset({(a, e.from_.column.lower()), (b, e.to.column.lower())})
        if identity in seen_pairs:
            continue  # the reverse of a join already kept (one-to-one is measured both ways)
        seen_pairs.add(identity)
        name = f"{names[a]}_{names[b]}"
        if any(r.name == name for r in relationships):
            name = f"{name}_{e.from_.column}"
        relationships.append(
            Relationship(
                name=name,
                from_=RelationshipEnd(table=names[a], column=e.from_.column),
                to=RelationshipEnd(table=names[b], column=e.to.column),
                cardinality=e.cardinality,
                evidence_ref=e.id,
            )
        )
    hidden = {(r.from_.table, r.from_.column) for r in relationships}
    measure_sources = {
        (names[t.lower()], c) for k in kpis for t, c, r in fields.get(k.name, []) if r == "measure" and t.lower() in names
    }

    # Tables.
    grain: dict[str, str | None] = {}
    for kpi in kpis:
        grain.setdefault(measure_table(kpi.name).lower(), kpi.grain)
    tables: list[ModelTable] = []
    for key in ordered:
        low, src = key.lower(), by_key[key.lower()]
        name = names[low]
        role = (
            "fact" if key in facts
            else "date" if date is not None and low == key_of(date).lower()
            else "bridge" if low in bridge_tables
            else "dimension"
        )
        columns = []
        for c in src.columns:
            mtype = model_type(c.data_type)
            if mtype is None:
                continue  # binary and other types the model can't hold
            columns.append(
                ModelColumn(
                    name=c.name,
                    source_column=c.name,
                    data_type=mtype,
                    format_string=DECIMAL_FORMAT if mtype == "decimal" else None,
                    is_hidden=(name, c.name) in hidden or (name, c.name) in measure_sources,
                )
            )
        tables.append(
            ModelTable(
                name=name,
                source=TableSource(schema=src.schema_name, table=src.table_name),
                role=role,
                grain=(grain.get(low) or inp.contract.grain) if role == "fact" else None,
                columns=columns,
                hierarchies=_hierarchy(src) if role == "date" else [],
            )
        )

    measures = [
        Measure(
            name=kpi.name,
            table=names[measure_table(kpi.name).lower()],
            kpi=kpi.name,
            formula_intent=kpi.formula_intent,
            dax=None,
            format_string=CURRENCY_FORMATS.get((kpi.currency or "").strip().upper(), DEFAULT_MEASURE_FORMAT),
            description=f"Approved: {kpi.formula_intent}" if kpi.formula_intent else None,
        )
        for kpi in kpis
    ]
    roles = [
        RlsRole(name=f"RLS{i}", description=s.description, applies_to=list(s.applies_to))
        for i, s in enumerate(inp.contract.security, 1)
    ]

    # Bridges that have no physical table yet are data-engineering work.
    for p in proposals:
        if not (p.table and p.table.lower() in by_key):
            actions.append(
                DataEngineeringAction(
                    id=f"DE{len(actions) + 1}",
                    kind="create_bridge_table",
                    description=(
                        f"Bridge {p.id} for {' and '.join(p.entities)}: "
                        + (f"generate it under approved rule {p.derivation_rule}" if p.derivation_rule else "mapping rules are required")
                    ),
                )
            )

    model = ModelSpec(
        run_id=inp.run_id,
        based_on={k: v for k, v in inp.based_on.items()},
        model_name=f"Nexora_{inp.run_id[:4]}",
        storage_mode="DirectLake",
        tables=tables,
        relationships=relationships,
        measures=measures,
        roles=roles,
    )
    plan = ExecutionPlan(
        run_id=inp.run_id,
        sources=[
            PlanSource(
                table=key,
                role=next(t.role for t in tables if t.name == names[key.lower()]),
                conformed=key.lower() in inp.conformed,
                exception_id=None if key.lower() in inp.conformed else inp.exceptions.get(key.lower()),
            )
            for key in ordered
        ],
        stages=[
            Stage(name="semantic_model", gate="G4", depends_on=["plan"]),
            Stage(name="report", gate="G5", depends_on=["semantic_model"]),
            Stage(name="deployment", gate="G6", depends_on=["report"]),
        ],
        target=Target(workspace_id=inp.workspace_id, kind="sandbox"),
        approvals_required=[
            RequiredApproval(gate="G3", roles=["bi_engineer"]),
            RequiredApproval(gate="G4", roles=["bi_engineer", "business_owner"]),
        ],
        bridges=[p.id for p in proposals],
        data_engineering_actions=actions,
    )
    return model, plan
