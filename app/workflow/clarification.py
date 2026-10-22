"""The clarification loop (T-203): answer or dismiss a question, edit a KPI.

An answer becomes a locked fact and the contract is recompiled, so the run moves
NEEDS_CLARIFICATION <-> CREATED through the compile service. Each call is a new graph
invocation; nothing waits inside a graph. Dismissing a question and editing a KPI need no
LLM: they write a new contract version directly. Only the store writes manifests.
"""

from pathlib import Path

from app.compiler.critical_terms import load_critical_terms, matching_kpi_index, term_for_fact
from app.compiler.requirement_compiler import LlmCall
from app.contracts import business_contract as bc
from app.store.base import RunStore
from app.workflow.compile_stage import (
    ARTIFACT,
    COMPILE_STATES,
    WrongState,
    compile_run,
    load_current_contract,
    save_contract_version,
)
from app.workflow.run import Run, utc_now

EDITABLE_KPI_FIELDS = (
    "formula_intent",
    "grain",
    "filters",
    "time_logic",
    "exclusions",
    "currency",
    "expected_examples",
    "rationale",
)


class NotFound(Exception):
    """A question or KPI that the contract doesn't have."""


class ContractLocked(Exception):
    def __init__(self, status: str):
        super().__init__(
            f"The contract is {status} and can't be changed. Reopen it through a new run or a rejection."
        )


class StaleVersion(Exception):
    def __init__(self, expected: str, current: str, what: str = "contract"):
        self.expected = expected
        self.current = current
        super().__init__(
            f"The {what} changed since you loaded it. Reload it and apply your edit again."
        )


class QuestionConflict(Exception):
    """The question's state doesn't allow the action (blocking, or not open)."""


def _editable(store: RunStore, runs_root: Path, run_id: str, action: str) -> tuple[Run, bc.BusinessContract]:
    run = store.get(run_id)
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no contract yet. Compile the requirement first.")
    if contract.status is not bc.ContractStatus.DRAFT:
        raise ContractLocked(contract.status.value)
    if run.state not in COMPILE_STATES:
        raise WrongState(action, run.state, COMPILE_STATES)
    return run, contract


def _question_index(contract: bc.BusinessContract, qid: str) -> int:
    for index, question in enumerate(contract.open_questions):
        if question.id == qid:
            return index
    raise NotFound(f"No question {qid!r} in the contract")


def _replace(contract: bc.BusinessContract, **changes) -> bc.BusinessContract:
    """A re-validated copy of the contract with top-level fields replaced."""
    data = contract.model_dump(mode="json")
    data.update(changes)
    return bc.BusinessContract.model_validate(data)


def answer_question(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    qid: str,
    answer: str,
    actor: str = "system",
    llm: LlmCall | None = None,
) -> tuple[Run, bc.BusinessContract]:
    """Store the answer, lock it as a fact and recompile. If the recompile fails, nothing
    is saved and the user can send the answer again."""
    run, contract = _editable(store, runs_root, run_id, "Answering a question")
    index = _question_index(contract, qid)

    questions = [q.model_dump(mode="json") for q in contract.open_questions]
    questions[index].update(status="answered", answer=answer, dismiss_reason=None)
    # Re-answering replaces the fact. The old answer stays in the earlier contract versions.
    facts = [
        f.model_dump(mode="json")
        for f in contract.locked_facts
        if f.source_question_id != qid
    ]
    facts.append(
        {"id": f"LF-{qid}", "text": answer, "source_question_id": qid, "locked_at": utc_now()}
    )
    answered = _replace(contract, open_questions=questions, locked_facts=facts)
    return compile_run(
        store,
        runs_root,
        run_id,
        actor=actor,
        llm=llm,
        previous=answered,
        action="Answering a question",
    )


def dismiss_question(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    qid: str,
    reason: str,
) -> tuple[Run, bc.BusinessContract]:
    """Close a non-blocking question with a reason. The state doesn't change."""
    run, contract = _editable(store, runs_root, run_id, "Dismissing a question")
    index = _question_index(contract, qid)
    question = contract.open_questions[index]
    if question.blocking:
        raise QuestionConflict(f"Question {qid} is blocking: blocking questions must be answered")
    if question.status is not bc.QuestionStatus.OPEN:
        raise QuestionConflict(f"Question {qid} is already {question.status.value}")

    questions = [q.model_dump(mode="json") for q in contract.open_questions]
    questions[index].update(status="dismissed", dismiss_reason=reason, answer=None)
    updated = _replace(
        contract, open_questions=questions, version=contract.version + 1
    )
    saved = save_contract_version(store, runs_root, run, updated)
    return store.get(run_id), saved


def edit_kpi(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    kpi_name: str,
    changes: dict,
    expected_hash: str,
) -> bc.BusinessContract:
    """Apply a partial KPI edit by the business owner (FR-003). `changes` holds only the
    fields to set. The state doesn't change."""
    run, contract = _editable(store, runs_root, run_id, "Editing a KPI")
    artifact = run.artifacts.get(ARTIFACT)
    if artifact is None or artifact.hash != expected_hash:
        raise StaleVersion(expected_hash, artifact.hash if artifact else "")

    updated = apply_kpi_edits(contract, {kpi_name: changes})
    return save_contract_version(store, runs_root, run, updated)


def apply_kpi_edits(
    contract: bc.BusinessContract, edits: dict[str, dict]
) -> bc.BusinessContract:
    """The contract with partial KPI edits applied (`edits` maps a KPI name, matched
    case-insensitively, to the fields to set), as the next version. Raises NotFound."""
    data = contract.model_dump(mode="json")
    config = None
    for kpi_name, changes in edits.items():
        key = kpi_name.strip().lower()
        index = next(
            (i for i, k in enumerate(contract.kpis) if k.name.strip().lower() == key), None
        )
        if index is None:
            raise NotFound(f"No KPI {kpi_name!r} in the contract")
        data["kpis"][index].update(
            {k: v for k, v in changes.items() if k in EDITABLE_KPI_FIELDS}
        )
        # A KPI defined by a locked fact keeps that fact in step with the owner's edit, so
        # the next recompile doesn't reset it.
        if "formula_intent" in changes and (changes["formula_intent"] or "").strip():
            config = config or load_critical_terms()
            for fact in data["locked_facts"]:
                term = term_for_fact(bc.LockedFact.model_validate(fact), config)
                if term is not None and matching_kpi_index(contract, term, config) == index:
                    fact["text"] = changes["formula_intent"].strip()
                    fact["locked_at"] = utc_now()
    data["version"] = contract.version + 1
    return bc.BusinessContract.model_validate(data)
