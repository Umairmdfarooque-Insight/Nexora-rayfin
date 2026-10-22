"""Review of the metadata inventory (T-304): Gate G2, exceptions, data gaps and Accept.

G2's checks are deterministic code (gates.run_g2). Exceptions and the Accept of the inventory
are approval records like the contract's, so the audit trail is one file. The run moves to
DATA_PROFILED only through Accept, after G2 passes. Only the store changes run state.
"""

import json
import uuid
from pathlib import Path

from pydantic import BaseModel

from app.contracts.metadata_inventory import MetadataInventory
from app.discovery.data_profiler import candidate_error_tables, candidate_tables
from app.store.base import RunStore
from app.workflow.approval import (
    Action,
    ApprovalRecord,
    Forbidden,
    GateFailed,
    Outcome,
    StaleVersion,
    _append,
    list_approvals,
)
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.conformed import load_conformed
from app.workflow.gates import GateResult, run_g2, save_gate_result
from app.workflow.harvest_stage import ARTIFACT, load_inventory
from app.workflow.roles import load_roles
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState

DATA_ROLE = "data_engineer"
EXCEPTION_PREFIX = "Exception:"
GAPS_FILE = "data_gaps.jsonl"
REVIEW_STATE = RunState.REQUIREMENT_APPROVED
GAP_STATES = (RunState.REQUIREMENT_APPROVED, RunState.DATA_PROFILED)


class NotACandidate(Exception):
    def __init__(self, table: str):
        self.table = table
        super().__init__(f"{table!r} is not a candidate table for this run's contract")


class DataGap(BaseModel):
    gap_id: str
    description: str
    raised_by: str
    at: str
    from_state: str


def _table_names(inventory: MetadataInventory, contract) -> tuple[list[str], list[str]]:
    return candidate_tables(contract, inventory), candidate_error_tables(contract, inventory)


def approved_exception_ids(store: RunStore, runs_root: Path, run_id: str) -> dict[str, str]:
    """Lower-cased table -> approval id of its (latest) approved exception."""
    return {
        r.artifact[len(EXCEPTION_PREFIX):].lower(): r.approval_id
        for r in list_approvals(store, runs_root, run_id)
        if r.artifact.startswith(EXCEPTION_PREFIX) and r.action is Action.ACCEPT
    }


def approved_exceptions(store: RunStore, runs_root: Path, run_id: str) -> set[str]:
    """Lower-cased tables with an approved exception."""
    return set(approved_exception_ids(store, runs_root, run_id))


def _inventory_ref(run: Run):
    ref = run.artifacts.get(ARTIFACT)
    if ref is None:
        raise NotFound("No metadata has been harvested for this run yet")
    return ref


def evaluate_g2(store: RunStore, runs_root: Path, run: Run) -> GateResult:
    inventory = load_inventory(runs_root, run.run_id)
    ref = _inventory_ref(run)
    contract = load_current_contract(runs_root, run.run_id)
    candidates, failed = _table_names(inventory, contract)
    return run_g2(
        inventory,
        ref.hash,
        candidates,
        failed,
        load_conformed(),
        approved_exceptions(store, runs_root, run.run_id),
    )


def check_g2(store: RunStore, runs_root: Path, run_id: str) -> GateResult:
    """Run Gate G2 on the current inventory and save the result."""
    result = evaluate_g2(store, runs_root, store.get(run_id))
    save_gate_result(runs_root, run_id, result)
    return result


def add_exception(
    store: RunStore, runs_root: Path, run_id: str, user: dict[str, str], table: str, rationale: str
) -> ApprovalRecord:
    """Approve a non-conformed candidate table. Only a data_engineer may."""
    if DATA_ROLE not in load_roles().roles_of(user["upn"]):
        raise Forbidden(DATA_ROLE, Action.ACCEPT, f"Only a {DATA_ROLE} can approve an exception")
    run = store.get(run_id)
    if run.state is not REVIEW_STATE:
        raise WrongState("Approving an exception", run.state, (REVIEW_STATE,))
    ref = _inventory_ref(run)
    inventory = load_inventory(runs_root, run_id)
    candidates, failed = _table_names(inventory, load_current_contract(runs_root, run_id))
    canonical = next((k for k in [*candidates, *failed] if k.lower() == table.strip().lower()), None)
    if canonical is None:
        raise NotACandidate(table)
    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        artifact=f"{EXCEPTION_PREFIX}{canonical}",
        artifact_version=ref.version,
        artifact_hash=ref.hash,
        action=Action.ACCEPT,
        role=DATA_ROLE,
        user={"oid": user["oid"], "upn": user["upn"]},
        at=utc_now(),
        comment=rationale,
    )
    _append(runs_root, record)
    return record


def accept_inventory(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None = None,
) -> Outcome:
    """Accept the inventory: G2 must pass, then the run moves to DATA_PROFILED."""
    if DATA_ROLE not in load_roles().roles_of(user["upn"]):
        raise Forbidden(DATA_ROLE, Action.ACCEPT, f"Only a {DATA_ROLE} can accept the metadata inventory")
    run = store.get(run_id)
    if run.state is not REVIEW_STATE:
        raise WrongState("Accepting the metadata inventory", run.state, (REVIEW_STATE,))
    ref = _inventory_ref(run)
    if ref.hash != expected_hash:
        raise StaleVersion(expected_hash, ref.hash)

    gate = evaluate_g2(store, runs_root, run)
    gate_ref = save_gate_result(runs_root, run_id, gate)
    if not gate.passed:
        raise GateFailed(gate)

    ref.status = "approved"
    ref.updated_at = utc_now()
    store.save(run)
    store.transition(run_id, RunState.DATA_PROFILED, "Metadata inventory approved", user["upn"])
    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        artifact=ARTIFACT,
        artifact_version=ref.version,
        artifact_hash=ref.hash,
        action=Action.ACCEPT,
        role=DATA_ROLE,
        user={"oid": user["oid"], "upn": user["upn"]},
        at=utc_now(),
        comment=comment,
        gate_result_ref=gate_ref,
    )
    _append(runs_root, record)
    return Outcome(approval=record, run=store.get(run_id), gate_result=gate)


def list_data_gaps(runs_root: Path, run_id: str) -> list[DataGap]:
    """The run's data gaps, oldest first. Never raises on a missing or damaged file."""
    path = Path(runs_root) / run_id / GAPS_FILE
    if not path.is_file():
        return []
    gaps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            gaps.append(DataGap.model_validate_json(line))
        except ValueError:
            continue
    return gaps


def raise_data_gap(
    store: RunStore, runs_root: Path, run_id: str, description: str, actor: str
) -> Run:
    """Record a data gap and move the run to NEEDS_INPUT (it returns to its previous state
    when input is provided)."""
    return raise_data_gaps(store, runs_root, run_id, [description], actor)


def raise_data_gaps(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    descriptions: list[str],
    actor: str,
    states: tuple[RunState, ...] = GAP_STATES,
) -> Run:
    """Record one or more data gaps and move the run to NEEDS_INPUT once."""
    run = store.get(run_id)
    if run.state not in states:
        raise WrongState("Raising a data gap", run.state, states)
    lines = [
        json.dumps(
            DataGap(
                gap_id=str(uuid.uuid4()),
                description=description,
                raised_by=actor,
                at=utc_now(),
                from_state=run.state.value,
            ).model_dump(),
            ensure_ascii=False,
        )
        + "\n"
        for description in descriptions
    ]
    summary = descriptions[0] if len(descriptions) == 1 else f"{len(descriptions)} data gaps"
    store.transition(run_id, RunState.NEEDS_INPUT, f"Data gap raised: {summary[:200]}", actor)
    path = Path(runs_root) / run_id / GAPS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:  # append only; bytes, so Windows keeps LF
        handle.write("".join(lines).encode("utf-8"))
    return store.get(run_id)
