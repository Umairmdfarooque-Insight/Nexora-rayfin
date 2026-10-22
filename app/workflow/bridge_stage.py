"""The bridge stage service (T-403): detect many-to-many needs, validate existing mapping
tables, save BridgeProposals.json and, when mapping rules are missing, move the run to
NEEDS_INPUT. Also the approval of one proposal (`BridgeProposal:<id>`).

A plain function, not a graph: the stage has no loop and no LLM. Nothing is generated: a
proposal is only a design. Only the store changes run state.
"""

import uuid
from pathlib import Path
from typing import Any, Callable

import pyodbc

from app.connectors.fabric_api import FabricUpstreamError
from app.contracts.bridge_proposal import SCHEMA_VERSION, BridgeProposal, BridgeProposals
from app.discovery.bridge_detection import detect, entity_key, measure_mapping
from app.discovery.derivation_rules import load_rules
from app.store.base import RunStore
from app.workflow.approval import (
    Action,
    AlreadyApproved,
    ApprovalRecord,
    Forbidden,
    Outcome,
    StaleVersion,
    UnsupportedAction,
    _append,
)
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState
from app.workflow.feasibility_stage import load_resolution
from app.workflow.harvest_stage import ARTIFACT as INVENTORY_ARTIFACT
from app.workflow.harvest_stage import ScopeMissing, canonical_hash, load_inventory, write_json
from app.workflow.inventory_review import raise_data_gaps
from app.workflow.roles import load_roles
from app.workflow.run import ArtifactRef, Run, RunScope, utc_now
from app.workflow.source_stage import ARTIFACT as RESOLUTION_ARTIFACT
from app.workflow.states import RunState

ARTIFACT = "BridgeProposals"
CURRENT = "BridgeProposals.json"
APPROVAL_PREFIX = "BridgeProposal:"
BRIDGE_STATES = (RunState.DATA_PROFILED,)
DECIDE_STATES = (RunState.DATA_PROFILED, RunState.NEEDS_INPUT)  # a missing rule elsewhere doesn't block deciding this one
BRIDGE_ROLES = ("bi_engineer", "business_owner")


class MappingRulesRequired(Exception):
    def __init__(self, message: str):
        super().__init__(message)


def load_proposals(runs_root: Path, run_id: str) -> BridgeProposals:
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        raise NotFound("No bridge proposals yet: POST /runs/{id}/plan/bridges first")
    return BridgeProposals.model_validate_json(path.read_text(encoding="utf-8"))


def _status(proposals: list[BridgeProposal]) -> str:
    if any(p.situation == "needs_rules" and p.status != "rejected" for p in proposals):
        return "needs_input"
    if proposals and all(p.status == "approved" for p in proposals):
        return "approved"
    return "draft"


def _write(store: RunStore, runs_root: Path, run: Run, proposals: BridgeProposals) -> None:
    data = proposals.to_dict()
    run_dir = Path(runs_root) / run.run_id
    write_json(run_dir / "bridges" / f"BridgeProposals.v{proposals.version}.json", data)
    write_json(run_dir / CURRENT, data)
    run.artifacts[ARTIFACT] = ArtifactRef(
        path=CURRENT,
        version=proposals.version,
        hash=canonical_hash(data),
        status=_status(proposals.proposals),
        based_on=proposals.based_on,
        updated_at=utc_now(),
    )
    store.save(run)


def _number(new: list[BridgeProposal], old: list[BridgeProposal]) -> list[BridgeProposal]:
    """Give each proposal an id. A proposal for the same entities keeps its earlier id, and its
    decision if nothing about it changed, so a re-run doesn't undo an approval."""
    previous = {entity_key(p.entities): p for p in old}
    used = {p.id for p in old}
    counter = max([0, *(int(p.id[1:]) for p in old if p.id[1:].isdigit())])
    out: list[BridgeProposal] = []
    for p in new:
        before = previous.get(entity_key(p.entities))
        if before is not None:
            p.id = before.id
            if (before.situation, before.table, before.derivation_rule) == (p.situation, p.table, p.derivation_rule):
                p.status = before.status
        else:
            counter += 1
            p.id = f"B{counter}"
            while p.id in used:
                counter += 1
                p.id = f"B{counter}"
        used.add(p.id)
        out.append(p)
    return out


def propose_bridges(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    sql_connector_for_scope: Callable[[RunScope], Any],
    actor: str = "system",
) -> tuple[BridgeProposals, Run]:
    """Detect bridge needs among the run's resolved entities and save the proposals."""
    run = store.get(run_id)
    if run.state not in BRIDGE_STATES:
        raise WrongState("Analyzing bridges", run.state, BRIDGE_STATES)
    if run.scope is None or run.scope.sql_endpoint is None:
        raise ScopeMissing("The run's Lakehouse has no SQL endpoint, so mapping tables can't be validated yet.")
    resolution = load_resolution(runs_root, run_id)
    inventory = load_inventory(runs_root, run_id)
    if not inventory.relationship_evidence:
        raise ScopeMissing("Relationships haven't been analyzed: POST /runs/{id}/relationships/analyze first.")
    rules = load_rules()  # CONFIG_ERROR before any query

    connector = sql_connector_for_scope(run.scope)
    try:
        with connector.connect() as connection:
            try:
                connection.timeout = 120
            except Exception:
                pass
            found = detect(inventory, resolution, rules, lambda c: measure_mapping(connection, inventory, c))
    except pyodbc.Error:
        raise FabricUpstreamError("Could not query the Lakehouse SQL endpoint") from None

    previous = Path(runs_root) / run_id / CURRENT
    old = BridgeProposals.model_validate_json(previous.read_text(encoding="utf-8")) if previous.is_file() else None
    proposals = _number(found, old.proposals if old else [])
    based_on = {RESOLUTION_ARTIFACT: canonical_hash(resolution.to_dict())}
    if INVENTORY_ARTIFACT in run.artifacts:
        based_on[INVENTORY_ARTIFACT] = run.artifacts[INVENTORY_ARTIFACT].hash

    if old is not None and old.based_on == based_on and old.proposals == proposals:
        result = old
    else:
        result = BridgeProposals(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            version=(old.version + 1) if old else 1,
            based_on=based_on,
            proposals=proposals,
        )
        _write(store, runs_root, run, result)

    needs = [p for p in result.proposals if p.situation == "needs_rules" and p.status != "rejected"]
    if needs:
        run = raise_data_gaps(
            store,
            runs_root,
            run_id,
            [
                f"Mapping rules are required for {' and '.join(p.entities)}: no mapping table or approved "
                "derivation rule links them. Provide the mapping rules or a maintained reference dataset."
                for p in needs
            ],
            actor,
        )
    else:
        run = store.get(run_id)
    return result, run


def decide_bridge(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    artifact: str,
    action: Action,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None = None,
) -> Outcome:
    """Accept or reject one proposal (`artifact` is `BridgeProposal:<id>`). A bi_engineer or a
    business_owner may. Nothing changes if any check fails."""
    if action not in (Action.ACCEPT, Action.REJECT):
        raise UnsupportedAction(artifact, action)
    held = [r for r in BRIDGE_ROLES if r in load_roles().roles_of(user["upn"])]
    if not held:
        raise Forbidden(BRIDGE_ROLES[0], action, "Only a bi_engineer or a business_owner can decide a bridge proposal")
    run = store.get(run_id)
    if run.state not in DECIDE_STATES:
        raise WrongState("Deciding a bridge proposal", run.state, DECIDE_STATES)
    ref = run.artifacts.get(ARTIFACT)
    proposals = load_proposals(runs_root, run_id)
    bridge_id = artifact[len(APPROVAL_PREFIX):]
    target = next((p for p in proposals.proposals if p.id == bridge_id), None)
    if target is None:
        raise NotFound(f"No bridge proposal {bridge_id!r} in this run")
    if ref is None or ref.hash != expected_hash:
        raise StaleVersion(expected_hash, ref.hash if ref else "")
    if target.status == "approved":
        raise AlreadyApproved(f"Bridge proposal {bridge_id} is already approved")
    if action is Action.ACCEPT:
        if target.situation == "needs_rules":
            raise MappingRulesRequired(
                f"Bridge proposal {bridge_id} can't be approved: mapping rules required for {' and '.join(target.entities)}"
            )
        if target.evidence.get("duplicate_pairs") and not target.deduplication_rule:
            raise MappingRulesRequired(
                f"Bridge proposal {bridge_id} can't be approved: the mapping table has "
                f"{target.evidence['duplicate_pairs']} duplicate pairs and no approved deduplication rule covers them"
            )

    target.status = "approved" if action is Action.ACCEPT else "rejected"
    proposals.version += 1
    _write(store, runs_root, run, proposals)
    ref = run.artifacts[ARTIFACT]
    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        artifact=artifact,
        artifact_version=ref.version,
        artifact_hash=ref.hash,
        action=action,
        role=held[0],
        user={"oid": user["oid"], "upn": user["upn"]},
        at=utc_now(),
        comment=comment,
    )
    _append(runs_root, record)
    return Outcome(approval=record, run=store.get(run_id))
