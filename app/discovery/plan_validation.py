"""Validation of ModelSpec and ExecutionPlan (T-404): a schema check (the typed models), reference
checks and policy rules. Deterministic code only. The result is a GateResult-like report
(gate `PLAN`); T-405 builds Gate G3 on top of the same facts.

Every failing check names the path of what is wrong, for example
`relationships[Sales_Product].to.column: Product.NoSuchColumn`.
"""

from app.contracts.business_contract import BusinessContract, FeasibilityStatus
from app.contracts.feasibility_report import FeasibilityReport
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.model_plan import ExecutionPlan, ModelSpec
from app.workflow.gates import GateCheck, GateResult
from app.workflow.run import utc_now

STRONG = ("high", "medium")


def _check(id_: str, description: str, problems: list[str], failed: str = "fail", **evidence) -> GateCheck:
    return GateCheck(
        id=id_,
        description=description,
        status="pass" if not problems else failed,
        evidence={"paths": problems, **evidence} if problems else evidence,
    )


def _references(model: ModelSpec, plan: ExecutionPlan, inventory: MetadataInventory | None) -> list[str]:
    problems: list[str] = []
    columns = {t.name: {c.name for c in t.columns} for t in model.tables}
    inv = (
        {f"{t.schema_name}.{t.table_name}".lower(): {c.name.lower() for c in t.columns} for t in inventory.tables}
        if inventory is not None
        else None
    )
    for t in model.tables:
        if t.source is None:
            continue
        key = f"{t.source.schema_name}.{t.source.table}".lower()
        if inv is not None and key not in inv:
            problems.append(f"tables[{t.name}].source: {t.source.schema_name}.{t.source.table} is not in the inventory")
            continue
        for c in t.columns:
            if inv is not None and c.source_column.lower() not in inv[key]:
                problems.append(f"tables[{t.name}].columns[{c.name}].source_column: {t.name}.{c.source_column} is not in the inventory")
        for h in t.hierarchies:
            for level in h.levels:
                if level.column not in columns[t.name]:
                    problems.append(f"tables[{t.name}].hierarchies[{h.name}].levels[{level.name}].column: {t.name}.{level.column}")
    for r in model.relationships:
        for side, end in (("from", r.from_), ("to", r.to)):
            if end.table not in columns:
                problems.append(f"relationships[{r.name}].{side}.table: {end.table}")
            elif end.column not in columns[end.table]:
                problems.append(f"relationships[{r.name}].{side}.column: {end.table}.{end.column}")
    for m in model.measures:
        if m.table not in columns:
            problems.append(f"measures[{m.name}].table: {m.table}")
    known = {f"{t.source.schema_name}.{t.source.table}".lower() for t in model.tables if t.source}
    for s in plan.sources:
        if s.table.lower() not in known:
            problems.append(f"sources[{s.table}]: not a table of the model")
    return problems


def _evidence_problems(model: ModelSpec, inventory: MetadataInventory | None) -> list[str]:
    evidence = {e.id: e for e in inventory.relationship_evidence} if inventory is not None else None
    problems = []
    for r in model.relationships:
        if (r.override_rationale or "").strip():
            continue
        if not r.evidence_ref:
            problems.append(f"relationships[{r.name}]: no evidence_ref and no override")
        elif evidence is not None:
            e = evidence.get(r.evidence_ref)
            if e is None:
                problems.append(f"relationships[{r.name}].evidence_ref: {r.evidence_ref} not found in the inventory")
            elif e.status != "candidate" or e.confidence not in STRONG:
                level = e.confidence if e.status == "candidate" else "rejected"
                problems.append(
                    f"relationships[{r.name}]: evidence {r.evidence_ref} is {level} and there is no override with a rationale"
                )
    return problems


def _duplicates(model: ModelSpec) -> list[str]:
    problems = []
    for label, names in (
        ("tables", [t.name for t in model.tables]),
        ("measures", [m.name for m in model.measures]),
        ("relationships", [r.name for r in model.relationships]),
        ("roles", [r.name for r in model.roles]),
    ):
        problems += [f"{label}[{n}]: name used more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    pairs: dict[frozenset, list[str]] = {}
    for r in model.relationships:
        pairs.setdefault(frozenset({r.from_.table, r.to.table}), []).append(r.name)
    problems += [
        f"relationships[{', '.join(names)}]: more than one relationship between the same two tables"
        for names in pairs.values()
        if len(names) > 1
    ]
    return problems


def _disconnected(model: ModelSpec) -> list[str]:
    if len(model.tables) < 2:
        return []
    neighbours: dict[str, set[str]] = {t.name: set() for t in model.tables}
    for r in model.relationships:
        if r.from_.table in neighbours and r.to.table in neighbours:
            neighbours[r.from_.table].add(r.to.table)
            neighbours[r.to.table].add(r.from_.table)
    facts = [t.name for t in model.tables if t.role == "fact"] or [model.tables[0].name]
    reached, queue = set(facts), list(facts)
    while queue:
        for other in neighbours[queue.pop()]:
            if other not in reached:
                reached.add(other)
                queue.append(other)
    return [f"tables[{t.name}]: no relationship connects it to a fact table" for t in model.tables if t.name not in reached]


def validate_plan(
    model: ModelSpec,
    plan: ExecutionPlan,
    inventory: MetadataInventory | None = None,
    feasibility: FeasibilityReport | None = None,
    contract: BusinessContract | None = None,
    artifact_hash: str = "",
) -> GateResult:
    """Run every check. Checks that need an input that wasn't given look only at what is there."""
    checks = [
        _check("PLAN.1", "Table, column and object references exist", _references(model, plan, inventory)),
        _check("PLAN.2", "Every relationship has evidence of high or medium confidence, or an override with a rationale", _evidence_problems(model, inventory)),
        _check("PLAN.3", "Names are unique and no two tables are related twice", _duplicates(model)),
        _check(
            "PLAN.4",
            "Direct Lake has no calculated tables" if model.storage_mode == "DirectLake" else "Import tables have a source",
            [f"tables[{t.name}].source: a calculated table has no Lakehouse source" for t in model.tables if t.source is None],
        ),
        _check(
            "PLAN.5",
            "Every source is conformed or has an approved exception",
            [f"sources[{s.table}]: not conformed and no exception" for s in plan.sources if not s.conformed and not s.exception_id],
        ),
    ]

    problems, warnings = [], []
    status = {k.kpi.lower(): k.status for k in feasibility.kpis} if feasibility is not None else None
    for m in model.measures:
        if status is not None and status.get(m.kpi.lower()) is not FeasibilityStatus.FEASIBLE:
            problems.append(f"measures[{m.name}].kpi: {m.kpi} is not a feasible KPI")
        if m.dax is not None:
            warnings.append(f"measures[{m.name}].dax: DAX is generated later (T-503)")
    checks.append(_check("PLAN.6", "Measures map to feasible KPIs", problems))
    checks.append(_check("PLAN.7", "Measures carry no DAX yet", warnings, failed="warning"))

    if contract is None:
        role_problems: list[str] = []
    elif len(model.roles) != len(contract.security):
        role_problems = [
            f"roles: {len(model.roles)} RLS role(s) but the contract's security section has {len(contract.security)}; "
            "roles come only from the contract"
        ]
    else:
        role_problems = []
    checks.append(_check("PLAN.8", "RLS roles come only from the contract's security section", role_problems))

    time_problems = []
    if contract is not None:
        wanted = {m.kpi.lower() for m in model.measures}
        needs_time = any((k.time_logic or "").strip() for k in contract.kpis if k.name.lower() in wanted)
        has_date = any(t.role == "date" for t in model.tables)
        has_action = any(a.kind == "create_date_table" for a in plan.data_engineering_actions)
        if needs_time and not (has_date or has_action):
            time_problems.append("tables: time logic needs a date table, and the plan has neither one nor an action to create it")
    checks.append(_check("PLAN.9", "Time logic has a date table or a data-engineering action to create one", time_problems))
    checks.append(_check("PLAN.10", "Every table is related to a fact table", _disconnected(model), failed="warning"))

    return GateResult(
        gate="PLAN",
        artifact="ModelSpec",
        artifact_hash=artifact_hash,
        passed=not any(c.status == "fail" for c in checks),
        checked_at=utc_now(),
        checks=checks,
    )
