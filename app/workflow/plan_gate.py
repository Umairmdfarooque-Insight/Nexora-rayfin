"""Gate G3 (spec §12, T-405): is the plan fit for a BI engineer to approve?

Deterministic code only. G3.1-G3.6 are the checks of the task; G3.7 adds the reference and
policy validation of T-404 (a plan that doesn't parse or points at objects that don't exist
can't be approved either).
"""

from app.contracts.business_contract import BusinessContract, FeasibilityStatus
from app.contracts.feasibility_report import FeasibilityReport
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.model_plan import ExecutionPlan, ModelSpec
from app.discovery.plan_validation import _check, _duplicates, _evidence_problems, _references
from app.workflow.gates import GateCheck, GateResult
from app.workflow.run import utc_now

PLAN_STAGE = "plan"


def _stage_problems(plan: ExecutionPlan) -> list[str]:
    problems: list[str] = []
    names = [s.name for s in plan.stages]
    problems += [f"stages[{n}]: name used more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    known = set(names) | {PLAN_STAGE}
    for s in plan.stages:
        if not s.gate.strip():
            problems.append(f"stages[{s.name}].gate: no gate")
        for dep in s.depends_on:
            if dep not in known:
                problems.append(f"stages[{s.name}].depends_on: unknown stage {dep!r}")
    graph = {s.name: [d for d in s.depends_on if d in known and d != PLAN_STAGE] for s in plan.stages}
    state: dict[str, int] = {}

    def visit(name: str) -> bool:
        if state.get(name) == 1:
            return True
        if state.get(name) == 2:
            return False
        state[name] = 1
        cyclic = any(visit(d) for d in graph.get(name, []))
        state[name] = 2
        return cyclic

    if any(visit(n) for n in graph):
        problems.append("stages: the dependencies form a cycle")
    if not any(a.gate == "G3" for a in plan.approvals_required):
        problems.append("approvals_required: no entry for gate G3")
    return problems


def _needed_tables(
    model: ModelSpec, contract: BusinessContract | None, feasibility: FeasibilityReport | None
) -> dict[str, tuple[str, set[str]]]:
    """Measure name -> (its table, the model tables its calculation needs)."""
    by_source = {f"{t.source.schema_name}.{t.source.table}".lower(): t.name for t in model.tables if t.source}
    needed_by_kpi = {
        k.kpi.lower(): {
            ".".join(n.mapped.split(".")[:2]).lower() for n in k.needed if n.mapped
        }
        for k in (feasibility.kpis if feasibility else [])
        if k.status is FeasibilityStatus.FEASIBLE
    }
    time_kpis = {k.name.lower() for k in (contract.kpis if contract else []) if (k.time_logic or "").strip()}
    dates = [t.name for t in model.tables if t.role == "date"]
    out: dict[str, tuple[str, set[str]]] = {}
    for m in model.measures:
        tables = {by_source[s] for s in needed_by_kpi.get(m.kpi.lower(), set()) if s in by_source}
        tables |= {f"?{s}" for s in needed_by_kpi.get(m.kpi.lower(), set()) if s not in by_source}
        if m.kpi.lower() in time_kpis:
            tables |= set(dates)
        out[m.name] = (m.table, tables)
    return out


def _measure_problems(
    model: ModelSpec, contract: BusinessContract | None, feasibility: FeasibilityReport | None
) -> list[str]:
    problems: list[str] = []
    status = {k.kpi.lower(): k.status for k in feasibility.kpis} if feasibility is not None else None
    for m in model.measures:
        if status is not None and status.get(m.kpi.lower()) is not FeasibilityStatus.FEASIBLE:
            problems.append(f"measures[{m.name}].kpi: {m.kpi} is not a feasible KPI")

    neighbours: dict[str, set[str]] = {t.name: set() for t in model.tables}
    for r in model.relationships:
        if r.from_.table in neighbours and r.to.table in neighbours:
            neighbours[r.from_.table].add(r.to.table)
            neighbours[r.to.table].add(r.from_.table)

    def reachable(start: str) -> set[str]:
        seen, queue = {start}, [start]
        while queue:
            for other in neighbours.get(queue.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        return seen

    for measure, (table, needed) in _needed_tables(model, contract, feasibility).items():
        if table not in neighbours:
            continue
        connected = reachable(table)
        for name in sorted(needed):
            if name.startswith("?"):
                problems.append(f"measures[{measure}]: needs {name[1:]}, which is not a table of the model")
            elif name != table and name not in connected:
                problems.append(f"measures[{measure}]: needs {name} related to {table}, but no relationship path connects them")
    return problems


def run_g3(
    model: ModelSpec,
    plan: ExecutionPlan,
    inventory: MetadataInventory | None,
    feasibility: FeasibilityReport | None,
    contract: BusinessContract | None,
    conformed: frozenset[str],
    excepted: set[str],
    artifact_hash: str,
) -> GateResult:
    """Gate G3 on the current ExecutionPlan and ModelSpec. `conformed` and `excepted` hold
    lower-cased "schema.table" names (read now, so a later change to the config counts)."""
    facts = [t for t in model.tables if t.role == "fact"]
    grain_problems = [f"tables[{t.name}].grain: no grain defined" for t in facts if not (t.grain or "").strip()]
    if not facts:
        grain_problems.append("tables: the model has no fact table")
    source_problems = [
        f"sources[{s.table}]: not conformed and no approved exception"
        for s in plan.sources
        if s.table.lower() not in conformed and s.table.lower() not in excepted
    ]
    blocking = [a for a in plan.data_engineering_actions if a.blocking and a.status == "open"]

    structural = (
        _references(model, plan, inventory)
        + _duplicates(model)
        + [f"tables[{t.name}].source: a calculated table has no Lakehouse source" for t in model.tables if t.source is None and model.storage_mode == "DirectLake"]
    )
    checks: list[GateCheck] = [
        _check("G3.1", "Fact grain is defined", grain_problems),
        _check("G3.2", "Every source is conformed, or has an approved exception", source_problems),
        _check("G3.3", "Every relationship has evidence or an override", _evidence_problems(model, inventory)),
        _check("G3.4", "Stage and gate dependencies are complete", _stage_problems(plan)),
        GateCheck(
            id="G3.5",
            description="No open data-engineering action blocks generation",
            status="fail" if blocking else "pass",
            evidence={
                "actions": [
                    {"id": a.id, "kind": a.kind, "owner": a.owner, "description": a.description} for a in blocking
                ]
            }
            if blocking
            else {},
        ),
        _check("G3.6", "Every measure maps to a feasible KPI and has the relationships it needs", _measure_problems(model, contract, feasibility)),
        _check("G3.7", "References resolve and names are unique", structural),
    ]
    return GateResult(
        gate="G3",
        artifact="ExecutionPlan",
        artifact_hash=artifact_hash,
        passed=not any(c.status == "fail" for c in checks),
        checked_at=utc_now(),
        checks=checks,
    )
