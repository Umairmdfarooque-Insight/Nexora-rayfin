"""The approval service (T-204, FR-018): Accept, Edit, Reject, Explain and Regenerate.

It is the only code that locks a contract. Gate G1 (deterministic checks) must pass before an
Accept, only a `business_owner` may Accept or Reject, and every action is appended to
`runs/<id>/approvals.jsonl` (append-only) with the user's UPN, a timestamp and the artifact
hash. Run state changes go through the store; the compile graph is only used by Regenerate.
"""

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.compiler.requirement_compiler import LlmCall
from app.contracts import business_contract as bc
from app.store.base import RunStore
from app.workflow.clarification import (
    ContractLocked,
    NotFound,
    StaleVersion,
    apply_kpi_edits,
)
from app.workflow.compile_stage import (
    ARTIFACT,
    COMPILE_STATES,
    WrongState,
    compile_run,
    load_current_contract,
    save_contract_version,
)
from app.workflow.gates import GateResult, run_g1, save_gate_result
from app.workflow.roles import load_roles
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState

APPROVALS_FILE = "approvals.jsonl"
OWNER = "business_owner"


class Action(str, Enum):
    ACCEPT = "ACCEPT"
    EDIT = "EDIT"
    REJECT = "REJECT"
    EXPLAIN = "EXPLAIN"
    REGENERATE = "REGENERATE"


class ApprovalRecord(BaseModel):
    approval_id: str
    run_id: str
    artifact: str
    artifact_version: int
    artifact_hash: str
    target: dict[str, Any] | None = None
    action: Action
    role: str | None = None
    user: dict[str, str]
    at: str
    comment: str | None = None
    gate_result_ref: str | None = None


@dataclass
class Outcome:
    approval: ApprovalRecord
    run: Run
    gate_result: GateResult | None = None
    explanation: dict | None = None


class Forbidden(Exception):
    def __init__(self, role: str, action: Action, message: str | None = None):
        self.role = role
        super().__init__(message or f"Only a {role} can {action.value.lower()} the contract")


class GateFailed(Exception):
    def __init__(self, result: GateResult):
        self.result = result
        self.failed = [c.id for c in result.checks if c.status == "fail"]
        super().__init__(f"Gate {result.gate} failed: {', '.join(self.failed)}")


class AlreadyApproved(Exception):
    def __init__(self, message: str = "The contract is already approved"):
        super().__init__(message)


class UnknownArtifact(Exception):
    def __init__(self, artifact: str):
        self.artifact = artifact
        super().__init__(f"Approvals are not supported for artifact {artifact!r}")


class UnsupportedAction(Exception):
    def __init__(self, artifact: str, action: Action):
        self.artifact = artifact
        super().__init__(f"{action.value} is not available for {artifact}")


def _approvals_path(runs_root: Path, run_id: str) -> Path:
    return Path(runs_root) / run_id / APPROVALS_FILE


def list_approvals(store: RunStore, runs_root: Path, run_id: str) -> list[ApprovalRecord]:
    store.get(run_id)  # RunNotFound for an unknown run
    path = _approvals_path(runs_root, run_id)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ApprovalRecord.model_validate_json(line) for line in lines if line.strip()]


def _append(runs_root: Path, record: ApprovalRecord) -> None:
    path = _approvals_path(runs_root, record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
    with open(path, "ab") as handle:  # append only; bytes, so Windows keeps LF
        handle.write(line.encode("utf-8"))


def _load(runs_root: Path, run_id: str) -> bc.BusinessContract:
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no contract yet. Compile the requirement first.")
    return contract


def check_g1(store: RunStore, runs_root: Path, run_id: str) -> GateResult:
    """Run Gate G1 on the current contract and save the result."""
    run = store.get(run_id)
    contract = _load(runs_root, run_id)
    artifact = run.artifacts.get(ARTIFACT)
    result = run_g1(contract, artifact.hash if artifact else "")
    save_gate_result(runs_root, run_id, result)
    return result


def explain(contract: bc.BusinessContract) -> dict:
    """A readable rationale for each KPI and question. Deterministic; no LLM involved."""
    kpis = []
    for kpi in contract.kpis:
        if (kpi.formula_intent or "").strip():
            parts = [f"{kpi.name} is defined as: {kpi.formula_intent.strip()}."]
            for label, value in (("Grain", kpi.grain), ("Currency", kpi.currency), ("Time logic", kpi.time_logic)):
                if (value or "").strip():
                    parts.append(f"{label}: {value.strip()}.")
            if kpi.exclusions:
                parts.append("Excludes: " + ", ".join(kpi.exclusions) + ".")
        else:
            parts = [f"{kpi.name} has no definition yet. The business owner must supply one before approval."]
        if kpi.rationale:
            parts.append(f"Why it is in the contract: {kpi.rationale.strip()}")
        kpis.append({"name": kpi.name, "status": kpi.status.value, "rationale": " ".join(parts)})
    questions = []
    for q in contract.open_questions:
        why = q.reason or (
            "Work can't proceed without this answer." if q.blocking else "Worth confirming."
        )
        questions.append(
            {
                "id": q.id,
                "text": q.text,
                "blocking": q.blocking,
                "status": q.status.value,
                "source": q.source.value,
                "rationale": f"Asked because: {why}",
            }
        )
    return {"kpis": kpis, "questions": questions}


def _next_reviewer_id(contract: bc.BusinessContract) -> str:
    numbers = [
        int(q.id[4:]) for q in contract.open_questions if q.id.startswith("REV-") and q.id[4:].isdigit()
    ]
    return f"REV-{max(numbers, default=0) + 1}"


def _perform(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    artifact: str,
    action: Action,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None = None,
    edits: dict | None = None,
    llm: LlmCall | None = None,
    role: str | None = None,
    definition_wrong: bool = False,
    target: dict[str, str] | None = None,
    report_services: Any = None,
) -> Outcome:
    """Carry out one approval action. `user` is {oid, upn}. Raises Forbidden, GateFailed,
    AlreadyApproved, ContractLocked, WrongState, StaleVersion, NotFound or UnknownArtifact;
    on any of them nothing is changed and nothing is recorded."""
    if artifact == "MetadataInventory":
        from app.workflow.inventory_review import accept_inventory  # avoids a circular import

        if action is not Action.ACCEPT:
            raise UnsupportedAction(artifact, action)
        return accept_inventory(store, runs_root, run_id, user, expected_hash, comment)
    if artifact == "Release":
        from app.workflow.promotion import decide_release  # avoids a circular import

        return decide_release(store, runs_root, run_id, action, user, expected_hash, comment)
    if artifact == "ExecutionPlan":
        from app.workflow.plan_review import decide_plan  # avoids a circular import

        return decide_plan(store, runs_root, run_id, action, user, expected_hash, comment)
    if artifact == "SemanticModel":
        from app.workflow.model_review import decide_model  # avoids a circular import

        return decide_model(store, runs_root, run_id, action, user, expected_hash, comment, role, definition_wrong)
    if artifact == "Report":
        from app.workflow.report_review import decide_report  # avoids a circular import

        return decide_report(store, runs_root, run_id, action, user, expected_hash, comment, role, target, report_services)
    if artifact.startswith("BridgeProposal:"):
        from app.workflow.bridge_stage import decide_bridge  # avoids a circular import

        return decide_bridge(store, runs_root, run_id, artifact, action, user, expected_hash, comment)
    if artifact != ARTIFACT:
        raise UnknownArtifact(artifact)
    run = store.get(run_id)
    contract = _load(runs_root, run_id)
    held = load_roles().roles_of(user["upn"])
    role = OWNER if OWNER in held else (held[0] if held else None)

    if action in (Action.ACCEPT, Action.REJECT) and OWNER not in held:
        raise Forbidden(OWNER, action)
    if action is not Action.EXPLAIN:
        if contract.status is bc.ContractStatus.APPROVED:
            raise AlreadyApproved() if action is Action.ACCEPT else ContractLocked("approved")
        if run.state not in COMPILE_STATES:
            raise WrongState(f"{action.value.title()} of the contract", run.state, COMPILE_STATES)
    current = run.artifacts.get(ARTIFACT)
    if current is None or current.hash != expected_hash:
        raise StaleVersion(expected_hash, current.hash if current else "")

    # Version and hash recorded: the version the action acted on, except Accept and Edit,
    # which record the version they produced.
    version, digest = current.version, current.hash
    gate: GateResult | None = None
    gate_ref: str | None = None
    explanation: dict | None = None
    actor = user["upn"]

    if action is Action.ACCEPT:
        gate = run_g1(contract, current.hash)
        gate_ref = save_gate_result(runs_root, run_id, gate)
        if not gate.passed:
            raise GateFailed(gate)
        if run.state is not RunState.CREATED:  # checked before anything is written
            raise WrongState("Accepting the contract", run.state, (RunState.CREATED,))
        approved = contract.model_copy(
            update={"status": bc.ContractStatus.APPROVED, "version": contract.version + 1}
        )
        bc.BusinessContract.model_validate(approved.model_dump(mode="json"))  # approval rules
        saved = save_contract_version(store, runs_root, run, approved, status="approved")
        version, digest = saved.version, run.artifacts[ARTIFACT].hash
        store.transition(run_id, RunState.REQUIREMENT_APPROVED, "Business contract approved", actor)
    elif action is Action.REJECT:
        data = contract.model_dump(mode="json")
        data["open_questions"].append(
            {
                "id": _next_reviewer_id(contract),
                "text": comment,
                "blocking": True,
                "reason": f"Rejected by {actor}",
                "source": "reviewer",
                "status": "open",
                "answer": None,
                "dismiss_reason": None,
            }
        )
        data["version"] = contract.version + 1
        save_contract_version(
            store, runs_root, run, bc.BusinessContract.model_validate(data)
        )
        if run.state is not RunState.NEEDS_CLARIFICATION:
            store.transition(
                run_id, RunState.NEEDS_CLARIFICATION, "Contract rejected by reviewer", actor
            )
    elif action is Action.EDIT:
        updated = apply_kpi_edits(contract, (edits or {}).get("kpis", {}))
        saved = save_contract_version(store, runs_root, run, updated)
        version, digest = saved.version, run.artifacts[ARTIFACT].hash
    elif action is Action.EXPLAIN:
        explanation = explain(contract)
    else:  # REGENERATE: recompile with the comment as guidance; locked facts are kept
        compile_run(
            store,
            runs_root,
            run_id,
            actor=actor,
            llm=llm,
            action="Regenerating the contract",
            guidance=comment,
        )

    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        artifact=artifact,
        artifact_version=version,
        artifact_hash=digest,
        action=action,
        role=role,
        user={"oid": user["oid"], "upn": actor},
        at=utc_now(),
        comment=comment,
        gate_result_ref=gate_ref,
    )
    _append(runs_root, record)
    return Outcome(approval=record, run=store.get(run_id), gate_result=gate, explanation=explanation)


def perform(store: RunStore, runs_root: Path, run_id: str, artifact: str, action: Action, *args: Any, **kwargs: Any) -> Outcome:
    """Carry out one approval action (see `_perform`). After an Accept, if the run was returned to an
    earlier stage (T-701), the artifacts that the newly approved one made stale are marked, and the
    return is closed once the run is approved again as far as it was."""
    outcome = _perform(store, runs_root, run_id, artifact, action, *args, **kwargs)
    from app.workflow import audit  # avoids a circular import

    audit.record(store, runs_root, run_id)  # T-703: the action and what it changed are in the audit log
    if outcome.approval.action is Action.ACCEPT:
        from app.workflow.impact import on_accept  # avoids a circular import

        on_accept(store, runs_root, run_id)
        audit.record(store, runs_root, run_id)
        outcome.run = store.get(run_id)
    return outcome
