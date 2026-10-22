"""Run states and the allowed transitions (CONTRACTS.md#run-states-and-transitions)."""

from enum import Enum


class RunState(str, Enum):
    CREATED = "CREATED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    REQUIREMENT_APPROVED = "REQUIREMENT_APPROVED"
    DATA_PROFILED = "DATA_PROFILED"
    PLAN_APPROVED = "PLAN_APPROVED"
    MODEL_GENERATING = "MODEL_GENERATING"
    MODEL_FAILED = "MODEL_FAILED"
    MODEL_APPROVED = "MODEL_APPROVED"
    REPORT_GENERATING = "REPORT_GENERATING"
    REPORT_FAILED = "REPORT_FAILED"
    REPORT_APPROVED = "REPORT_APPROVED"
    DEPLOYING = "DEPLOYING"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"
    NEEDS_INPUT = "NEEDS_INPUT"


S = RunState

# NEEDS_INPUT can be entered from every state except COMPLETED (added below).
# Leaving NEEDS_INPUT is decided by the run's stored resume_state, so only
# CREATED is listed statically for it.
ALLOWED: dict[RunState, set[RunState]] = {
    S.CREATED: {S.NEEDS_CLARIFICATION, S.REQUIREMENT_APPROVED},
    S.NEEDS_CLARIFICATION: {S.CREATED},
    S.REQUIREMENT_APPROVED: {S.DATA_PROFILED, S.CREATED},
    S.DATA_PROFILED: {S.PLAN_APPROVED, S.REQUIREMENT_APPROVED, S.CREATED},
    S.PLAN_APPROVED: {S.MODEL_GENERATING, S.DATA_PROFILED, S.CREATED},
    S.MODEL_GENERATING: {S.MODEL_FAILED, S.MODEL_APPROVED},
    S.MODEL_FAILED: {S.MODEL_GENERATING},
    S.MODEL_APPROVED: {S.REPORT_GENERATING, S.CREATED, S.PLAN_APPROVED, S.DATA_PROFILED},
    S.REPORT_GENERATING: {S.REPORT_FAILED, S.REPORT_APPROVED},
    S.REPORT_FAILED: {S.REPORT_GENERATING, S.MODEL_GENERATING},
    S.REPORT_APPROVED: {
        S.DEPLOYING,
        S.CREATED,
        S.PLAN_APPROVED,
        S.DATA_PROFILED,
        S.MODEL_GENERATING,
    },
    S.DEPLOYING: {S.COMPLETED, S.ROLLED_BACK, S.NEEDS_INPUT},
    S.ROLLED_BACK: {S.DEPLOYING},
    S.COMPLETED: set(),
    S.NEEDS_INPUT: {S.CREATED},
}

for _state in RunState:
    if _state is not S.COMPLETED:
        ALLOWED[_state].add(S.NEEDS_INPUT)
# A state can't move to itself, so NEEDS_INPUT -> NEEDS_INPUT stays illegal.
ALLOWED[S.NEEDS_INPUT].discard(S.NEEDS_INPUT)


def can_transition(
    a: RunState, b: RunState, resume_state: RunState | None = None
) -> bool:
    """True if a run in state `a` may move to `b`.

    `resume_state` matters only when `a` is NEEDS_INPUT: the run may also
    return to the state it was in before.
    """
    if a == b:
        return False
    if b in ALLOWED[a]:
        return True
    return a is S.NEEDS_INPUT and resume_state is not None and b == resume_state
