"""The profile stage service (T-302): check the run, pick the tables, run the profiler and
write the profiles into the run's MetadataInventory.json.

A plain function, not a graph: the stage has no loop (CONTRACTS.md#stage-graphs). It never
changes the run state; T-304 (Gate G2) moves the run to DATA_PROFILED.
"""

from pathlib import Path
from typing import Any, Callable

import pyodbc

from app.connectors.fabric_api import FabricUpstreamError
from app.contracts.metadata_inventory import MetadataInventory, TableProfile
from app.discovery.data_profiler import candidate_tables, profile_tables, table_key
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.harvest_stage import ScopeMissing, load_inventory, save_inventory
from app.workflow.run import RunScope
from app.workflow.states import RunState

PROFILE_STATES = (RunState.REQUIREMENT_APPROVED, RunState.DATA_PROFILED)


def profile_run(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    sql_connector_for_scope: Callable[[RunScope], Any],
    tables: list[str] | None = None,
) -> dict[str, TableProfile]:
    """Profile the run's candidate tables plus any named in `tables` ("schema.table").

    Profiles are merged into the current inventory (tables profiled earlier keep theirs). The
    inventory keeps its harvest version; its artifact hash and timestamp are updated.
    """
    run = store.get(run_id)
    if run.state not in PROFILE_STATES:
        raise WrongState("Profiling data", run.state, PROFILE_STATES)
    if run.scope is None or run.scope.sql_endpoint is None:
        raise ScopeMissing(
            "The run's Lakehouse has no SQL endpoint, so its data can't be profiled yet."
        )
    inventory = load_inventory(runs_root, run_id)

    known = {table_key(t).lower(): table_key(t) for t in inventory.tables}
    selected = candidate_tables(load_current_contract(runs_root, run_id), inventory)
    for name in tables or []:
        canonical = known.get(name.strip().lower())
        if canonical is None:
            raise NotFound(f"Table {name!r} is not in the harvested inventory; harvest it first")
        if canonical not in selected:
            selected.append(canonical)
    if not selected:
        return {}

    try:
        profiles = profile_tables(sql_connector_for_scope(run.scope), inventory, selected)
    except pyodbc.Error:
        raise FabricUpstreamError("Could not connect to the Lakehouse SQL endpoint") from None

    for table in inventory.tables:
        profile = profiles.get(table_key(table))
        if profile is not None:
            table.profile = profile
    save_inventory(store, runs_root, run, inventory)
    return profiles


def summarize(profiles: dict[str, TableProfile]) -> dict[str, int]:
    return {
        "profiled": sum(1 for p in profiles.values() if p.status != "error"),
        "warnings": sum(1 for p in profiles.values() if p.status == "warning"),
        "errors": sum(1 for p in profiles.values() if p.status == "error"),
    }
