"""The harvest stage service (T-301): check the run, harvest, write the inventory version,
register the artifact and, for a re-harvest, move the state back.

A plain function, not a graph: the stage has no loop (CONTRACTS.md#stage-graphs).
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from app.fsutil import replace_file
from app.contracts.metadata_inventory import (
    SCHEMA_VERSION,
    InventoryScope,
    MetadataInventory,
)
from app.discovery.harvester import harvest_tables, resolve_schemas
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState
from app.workflow.run import ArtifactRef, Run, RunScope, utc_now
from app.workflow.states import RunState

ARTIFACT = "MetadataInventory"
CURRENT = "MetadataInventory.json"
HARVEST_STATES = (RunState.REQUIREMENT_APPROVED, RunState.DATA_PROFILED)
_VERSION_FILE = re.compile(r"MetadataInventory\.v(\d+)\.json$")


class ScopeMissing(Exception):
    def __init__(self, message: str | None = None):
        super().__init__(
            message or "The run has no Lakehouse scope yet. Set it with PUT /runs/{id}/scope first."
        )


def canonical_hash(data: dict) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    replace_file(tmp, path)


def _next_version(run: Run, inventories_dir: Path) -> int:
    existing = [
        int(m.group(1))
        for p in inventories_dir.glob("MetadataInventory.v*.json")
        if (m := _VERSION_FILE.search(p.name))
    ]
    previous = run.artifacts.get(ARTIFACT)
    return max([0, *existing, previous.version if previous else 0]) + 1


def harvest_run(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    connector_for_scope: Callable[[RunScope], Any],
    schemas: list[str] | None = None,
    actor: str = "system",
) -> tuple[Run, MetadataInventory]:
    """Harvest the run's scope into the next MetadataInventory version.

    Allowed in REQUIREMENT_APPROVED, and in DATA_PROFILED for a re-harvest (which moves the
    run back to REQUIREMENT_APPROVED). Nothing is written if a schema is unknown or the
    Lakehouse can't be listed.
    """
    run = store.get(run_id)
    if run.state not in HARVEST_STATES:
        raise WrongState("Harvesting metadata", run.state, HARVEST_STATES)
    if run.scope is None:
        raise ScopeMissing()

    connector = connector_for_scope(run.scope)
    wanted = list(schemas or run.scope.schemas)
    resolved = resolve_schemas(connector, wanted)
    tables, errors, warnings = harvest_tables(connector, resolved)

    run_dir = Path(runs_root) / run_id
    inventories_dir = run_dir / "inventories"
    version = _next_version(run, inventories_dir)
    inventory = MetadataInventory(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        version=version,
        harvested_at=utc_now(),
        scope=InventoryScope(
            workspace_id=run.scope.workspace_id,
            lakehouse_id=run.scope.lakehouse_id,
            schemas=resolved,
        ),
        tables=tables,
        relationship_evidence=[],
        errors=errors,
        warnings=warnings,
    )
    data = inventory.to_dict()
    write_json(inventories_dir / f"MetadataInventory.v{version}.json", data)
    write_json(run_dir / CURRENT, data)

    contract = run.artifacts.get("BusinessContract")
    run.artifacts[ARTIFACT] = ArtifactRef(
        path=CURRENT,
        version=version,
        hash=canonical_hash(data),
        status="draft",
        based_on={"BusinessContract": contract.hash} if contract else {},
        updated_at=utc_now(),
    )
    store.save(run)

    if run.state is RunState.DATA_PROFILED:
        store.transition(run_id, RunState.REQUIREMENT_APPROVED, "Metadata re-harvested", actor)
    return store.get(run_id), inventory


def save_inventory(store: RunStore, runs_root: Path, run: Run, inventory: MetadataInventory) -> None:
    """Write `inventory` over its current file and its same-version copy (after profiles or
    evidence were added) and update the artifact's hash. The harvest version doesn't change."""
    data = inventory.to_dict()
    run_dir = Path(runs_root) / run.run_id
    write_json(run_dir / CURRENT, data)
    write_json(run_dir / "inventories" / f"MetadataInventory.v{inventory.version}.json", data)
    ref = run.artifacts[ARTIFACT]
    ref.hash = canonical_hash(data)
    ref.updated_at = utc_now()
    store.save(run)


def load_inventory(runs_root: Path, run_id: str) -> MetadataInventory:
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        raise NotFound("No metadata has been harvested for this run yet")
    return MetadataInventory.model_validate_json(path.read_text(encoding="utf-8"))


def summary(inventory: MetadataInventory) -> dict[str, int]:
    return {
        "tables": len(inventory.tables),
        "columns": sum(len(t.columns) for t in inventory.tables),
        "errors": len(inventory.errors),
    }
