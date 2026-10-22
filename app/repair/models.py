"""Shapes of the repair service (T-505)."""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.workflow.states import RunState


@dataclass(frozen=True)
class ClassifiedError:
    """A failed check, normalized: its fingerprint, its repair code and its class by policy."""

    fingerprint: str
    object: str
    check_id: str
    code: str  # the repair code: the key into config/repair_policy.yaml
    classification: str  # deterministic | llm | user_input
    message: str


class RepairAttempt(BaseModel):
    attempt: int
    fixed: list[str] = Field(default_factory=list)  # fingerprints that are gone after the attempt
    remaining: list[str] = Field(default_factory=list)  # fingerprints still failing
    classification: dict[str, str] = Field(default_factory=dict)  # of the remaining ones
    version: int  # the artifact version this attempt left in place
    discarded: bool = False  # the attempt wrote nothing: no fix, or the scope guard dropped it
    note: str | None = None


class RepairResult(BaseModel):
    attempts: list[RepairAttempt]
    final_state: RunState
    outcome: str  # passed | limit_reached | no_progress | user_input_only
    message: str | None = None
