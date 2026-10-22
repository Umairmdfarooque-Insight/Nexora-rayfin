"""The bridge generation stage (T-506): put an approved bridge into the model and validate it.

For an approved proposal (T-403) in a run whose model has been generated:
- `existing_table`: the mapping table becomes a `bridge` table with two relationships. If it has
  duplicate pairs and an approved deduplication rule allows it, a deterministic notebook first
  materializes a `SELECT DISTINCT` copy of it; without such a rule duplicates fail validation.
- `derivation_rule`: the model drafts Spark SQL from the approved rule (`model/bridge_sql.py`),
  code checks it, and a notebook materializes the table.
Notebooks run only in the sandbox workspace, write only `nexora_generated` in the Lakehouse they are
attached to, and fail on an empty result. The new table is harvested into the inventory, the
ModelSpec gets the next version, T-502 writes the next TMDL version and G4 validates it, including
G4.BRIDGE.*. A notebook that fails changes nothing. A failed validation moves the run to
MODEL_FAILED, as the validation endpoint does.
"""

from pathlib import Path
from typing import Any

from app.compiler.critical_terms import ConfigError
from app.contracts.metadata_inventory import InventoryScope, InventoryTable
from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, save_yaml, to_data
from app.contracts.validation_report import ValidationRun, load_report
from app.deploy.fabric_items import FabricOperationFailed, FabricItemsClient, TargetNotAllowed
from app.discovery.derivation_rules import load_rules
from app.discovery.harvester import harvest_tables
from app.model.bridge_model import BridgeError, apply_bridge, find_sides
from app.model.bridge_notebook import TARGET_SCHEMA, build_notebook, dedup_select, target_table, write_notebook
from app.model.bridge_sql import LlmCall, propose_bridge_sql
from app.store.base import RunStore
from app.workflow.bridge_stage import MappingRulesRequired, load_proposals
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState
from app.workflow.feasibility_stage import load_resolution
from app.workflow.harvest_stage import canonical_hash, load_inventory, save_inventory
from app.workflow.llm_cache import CachedLlm, read_answers, write_answers
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash, write_model_version
from app.workflow.model_stage import ModelSpecInvalid
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE, PLAN_FILE, _validation
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState
from app.workflow.validate_stage import Environment, register_report, run_validation

BRIDGE_STATES = (RunState.MODEL_GENERATING,)
CACHE = "bridge_suggestions.json"


class BridgeNotApproved(Exception):
    def __init__(self, bridge_id: str):
        self.bridge_id = bridge_id
        super().__init__(f"Bridge {bridge_id} is not approved: approve the proposal first")


def _materialize(
    store: RunStore, runs_root: Path, run_id: str, env: Environment, items_client: FabricItemsClient,
    lakehouse_connector_factory: Any, bridge_id: str, select_sql: str, table: str, description: str, expected: tuple[str, str],
) -> InventoryTable:
    """Run the notebook that creates `nexora_generated.<table>` and add the table to the inventory."""
    if env.scope.workspace_id.casefold() != env.workspace_id.casefold():
        raise TargetNotAllowed("A bridge table can only be materialized in the sandbox Lakehouse, and this run's Lakehouse is not in the sandbox workspace")
    notebook = build_notebook(
        bridge_id, select_sql, table, description,
        {"id": env.scope.lakehouse_id, "name": env.scope.lakehouse_name, "workspace_id": env.scope.workspace_id},
    )
    path = Path(runs_root) / run_id / "bridges" / f"{bridge_id}.ipynb"
    write_notebook(notebook, path)
    item = items_client.upsert_notebook(env.workspace_id, f"Nexora_{run_id[:8]}_{bridge_id}", path)
    items_client.run_notebook(env.workspace_id, item["item_id"])

    tables, _, _ = harvest_tables(lakehouse_connector_factory(env.scope), [TARGET_SCHEMA])
    found = next((t for t in tables if t["table_name"].casefold() == table.casefold()), None)
    if found is None:
        raise FabricOperationFailed(f"The notebook ran, but {TARGET_SCHEMA}.{table} was not found in the Lakehouse")
    new = InventoryTable.model_validate(found)
    have = {c.name.casefold() for c in new.columns}
    missing = [c for c in expected if c.casefold() not in have]
    if missing:
        raise BridgeError(f"The derived table lacks the column(s) {', '.join(missing)}: it has {', '.join(sorted(c.name for c in new.columns))}")

    inventory = load_inventory(runs_root, run_id)
    inventory.tables = [t for t in inventory.tables if t.schema_name.casefold() != TARGET_SCHEMA] + [new]
    if TARGET_SCHEMA.casefold() not in {s.casefold() for s in inventory.scope.schemas}:
        inventory.scope = InventoryScope(**{**inventory.scope.model_dump(), "schemas": [*inventory.scope.schemas, TARGET_SCHEMA]})
    save_inventory(store, runs_root, store.get(run_id), inventory)
    return new


def _baseline(run_dir: Path, run_id: str, version: int) -> dict[str, float | None]:
    """The measure totals of the latest passing validation of an earlier model version."""
    for entry in reversed(load_report(run_dir / "ValidationReport.json", run_id).runs_of("SemanticModel")):
        if entry.passed and entry.artifact_version < version:
            return {
                c.object.partition(":")[2]: c.evidence.get("value")
                for c in entry.checks
                if c.id == "G4.DAX" and c.object.startswith("measure:") and c.status != "fail"
            }
    return {}


def generate_bridge(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    bridge_id: str,
    *,
    items_client: FabricItemsClient,
    pbi: Any,
    sql_connector_factory: Any,
    recon_llm: LlmCall,
    bridge_llm: LlmCall,
    lakehouse_connector_factory: Any,
    user_upn: str | None = None,
    actor: str = "system",
) -> tuple[int, ValidationRun, Run]:
    """`POST /runs/{id}/model/bridges/{bridge_id}/generate`: returns the new TMDL version, its
    validation and the run."""
    run = store.get(run_id)
    if run.state not in BRIDGE_STATES:
        raise WrongState("Generating a bridge", run.state, BRIDGE_STATES)
    proposals = load_proposals(runs_root, run_id)
    proposal = next((p for p in proposals.proposals if p.id == bridge_id), None)
    if proposal is None:
        raise NotFound(f"There is no bridge proposal {bridge_id!r} in this run")
    if proposal.situation == "needs_rules":
        raise MappingRulesRequired(f"Bridge {bridge_id} needs mapping rules before it can be generated")
    if proposal.status != "approved":
        raise BridgeNotApproved(bridge_id)
    if TMDL_ARTIFACT not in run.artifacts or MODEL_ARTIFACT not in run.artifacts:
        raise NotFound("The model hasn't been generated yet: POST /runs/{id}/model/generate first")

    env = Environment(store, runs_root, run_id)
    resolution = load_resolution(runs_root, run_id)
    run_dir = Path(runs_root) / run_id

    if proposal.situation == "existing_table":
        mapping = proposal.table
        sides = find_sides(proposal, resolution, env.inventory, env.spec, mapping)
        columns = (sides[0].bridge_column, sides[1].bridge_column)
        schema, _, name = mapping.partition(".")
        rationale = None
        if int(proposal.evidence.get("duplicate_pairs") or 0) > 0:
            if not proposal.deduplication_rule:
                raise MappingRulesRequired(f"Bridge {bridge_id} has duplicate pairs and no approved deduplication rule")
            source = _materialize(
                store, runs_root, run_id, env, items_client, lakehouse_connector_factory, bridge_id,
                dedup_select(schema, name, columns), target_table(bridge_id, name),
                f"Removes the duplicate pairs of {mapping} under the approved deduplication rule {proposal.deduplication_rule}.", columns,
            )
        else:
            source = next(t for t in env.inventory.tables if f"{t.schema_name}.{t.table_name}".lower() == mapping.lower())
    else:
        rule = next((r for r in load_rules() if r.id == proposal.derivation_rule), None)
        if rule is None:
            raise ConfigError(f"Derivation rule {proposal.derivation_rule} of bridge {bridge_id} is not in derivation_rules.yaml")
        sides = find_sides(proposal, resolution, env.inventory, env.spec, None)
        keys = {s.entity: s.key for s in sides}
        columns = (sides[0].key, sides[1].key)
        cache = read_answers(run_dir, CACHE)
        cached = CachedLlm(bridge_llm, cache)
        proposed = propose_bridge_sql(rule, {s.entity: s.source_key for s in sides}, keys, env.inventory, cached)
        source = _materialize(
            store, runs_root, run_id, env, items_client, lakehouse_connector_factory, bridge_id,
            proposed.select_sql, target_table(bridge_id, "_".join(proposal.entities)),
            f"Derived under the approved rule {rule.id}: {rule.description}", columns,
        )
        write_answers(run_dir, CACHE, {**cache, cached.last_key: cached.last_raw})
        rationale = f"Generated from the approved derivation rule {rule.id}: {rule.description}"

    # The model with the bridge: validated before anything is written.
    env = Environment(store, runs_root, run_id)  # the inventory may have a new table
    candidate = apply_bridge(env.spec, sides, source, columns, rationale)
    plan = load_yaml(ExecutionPlan, run_dir / PLAN_FILE)
    result = _validation(runs_root, run_id, candidate, plan)
    if not result.passed:
        raise ModelSpecInvalid(result)
    candidate.version = env.spec.version + 1
    save_yaml(candidate, run_dir / MODEL_FILE)
    save_yaml(candidate, run_dir / "plans" / f"ModelSpec.v{candidate.version}.yaml")
    run = store.get(run_id)
    ref = run.artifacts[MODEL_ARTIFACT]
    ref.version, ref.hash, ref.updated_at = candidate.version, canonical_hash(to_data(candidate)), utc_now()
    store.save(run)

    version, _ = write_model_version(store, runs_root, run_id)
    env = Environment(store, runs_root, run_id)
    folder = run_dir / "model" / f"v{version}"
    entry = run_validation(
        env, folder, version, folder_hash(folder),
        items_client=items_client, pbi=pbi, sql_connector_factory=sql_connector_factory,
        recon_llm=recon_llm, user_upn=user_upn, baseline=_baseline(run_dir, run_id, version),
    )
    register_report(store, runs_root, run_id, entry)
    run = store.get(run_id)
    if not entry.passed:
        failed = sum(1 for c in entry.checks if c.status == "fail")
        run = store.transition(run_id, RunState.MODEL_FAILED, f"Bridge {bridge_id} validation failed: {failed} check(s)", actor)
    return version, entry, run
