"""The compile stage service (T-202): check the run state, run the compile graph, write the
contract version and make the state transition.

The graph (app/graphs/compile.py) only proposes and validates. This service owns the
artifact files, the manifest entry and the transition, as CONTRACTS.md#stage-graphs requires.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.compiler.requirement_compiler import LlmCall, compile_requirement
from app.contracts import business_contract as bc
from app.llm.errors import LlmInvalidOutput
from app.store.base import RunStore
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.states import RunState

ARTIFACT = "BusinessContract"
CURRENT = "BusinessContract.yaml"
COMPILE_STATES = (RunState.CREATED, RunState.NEEDS_CLARIFICATION)
_VERSION_FILE = re.compile(r"BusinessContract\.v(\d+)\.yaml$")


class WrongState(Exception):
    def __init__(self, action: str, state: RunState, allowed: tuple[RunState, ...]):
        self.state = state
        self.allowed = allowed
        names = " or ".join(s.value for s in allowed)
        super().__init__(f"{action} is only allowed in {names}, not {state.value}")


def load_current_contract(runs_root: Path, run_id: str) -> bc.BusinessContract | None:
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        return None
    return bc.load(path)


def current_contract(runs_root: Path, run_id: str) -> bc.BusinessContract | None:
    """The run's current contract for the run detail. None if there is none or it can't be
    read. Never raises."""
    try:
        return load_current_contract(runs_root, run_id)
    except (bc.ContractInvalid, OSError):
        return None


def open_questions(runs_root: Path, run_id: str) -> list[dict]:
    """Open questions of the run's current contract, for the run detail. Never raises."""
    contract = current_contract(runs_root, run_id)
    if contract is None:
        return []
    return [
        q.model_dump(mode="json")
        for q in contract.open_questions
        if q.status is bc.QuestionStatus.OPEN
    ]


def _next_version(contracts_dir: Path, wanted: int) -> int:
    existing = [
        int(m.group(1))
        for p in contracts_dir.glob("BusinessContract.v*.yaml")
        if (m := _VERSION_FILE.search(p.name))
    ]
    return max([wanted, *(n + 1 for n in existing)])


def _write_compile_log(
    runs_root: Path, run_id: str, attempts: list[dict] | None = None, warnings: list[str] | None = None
) -> None:
    logs = Path(runs_root) / run_id / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body: dict = {"run_id": run_id, "stage": "compile", "at": utc_now()}
    if attempts is not None:
        body["attempts"] = attempts
    if warnings:
        body["warnings"] = warnings
    (logs / f"compile-{stamp}.json").write_text(
        json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_contract_version(
    store: RunStore,
    runs_root: Path,
    run: Run,
    contract: bc.BusinessContract,
    status: str = "draft",
) -> bc.BusinessContract:
    """Write `contract` as the next version file and as the current file, and register the
    artifact. Returns the contract with its final version number. Doesn't touch the state."""
    run_dir = Path(runs_root) / run.run_id
    contracts_dir = run_dir / "contracts"
    version = _next_version(contracts_dir, contract.version)
    if version != contract.version:
        contract = contract.model_copy(update={"version": version})

    digest = bc.save(contract, contracts_dir / f"BusinessContract.v{version}.yaml")
    bc.save(contract, run_dir / CURRENT)

    run.artifacts[ARTIFACT] = ArtifactRef(
        path=CURRENT,
        version=version,
        hash=digest,
        status=status,
        based_on={},
        updated_at=utc_now(),
    )
    store.save(run)
    return contract


_LOAD = object()


def compile_run(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    actor: str = "system",
    llm: LlmCall | None = None,
    previous=_LOAD,
    action: str = "Compiling the requirement",
    guidance: str | None = None,
) -> tuple[Run, bc.BusinessContract]:
    """Compile the run's prompt into the next contract version.

    `previous` defaults to the run's current contract file; the clarification loop passes
    the current contract with the user's answer already applied. State afterwards:
    NEEDS_CLARIFICATION while a blocking question is open, otherwise CREATED. A state that
    doesn't change adds no history entry.
    """
    run = store.get(run_id)
    if run.state not in COMPILE_STATES:
        raise WrongState(action, run.state, COMPILE_STATES)

    if previous is _LOAD:
        previous = load_current_contract(runs_root, run_id)
    warnings: list[str] = []
    try:
        contract = compile_requirement(
            run, previous, llm=llm, warnings=warnings, guidance=guidance
        )
    except LlmInvalidOutput as exc:
        _write_compile_log(runs_root, run_id, attempts=exc.attempts)
        raise
    if warnings:
        _write_compile_log(runs_root, run_id, warnings=warnings)

    contract = save_contract_version(store, runs_root, run, contract)

    blocking = len(bc.open_blocking_questions(contract))
    target = RunState.NEEDS_CLARIFICATION if blocking else RunState.CREATED
    if target is not run.state:
        reason = (
            f"{blocking} blocking question(s) open"
            if blocking
            else "No blocking questions; draft contract ready"
        )
        store.transition(run_id, target, reason, actor)
    return store.get(run_id), contract
