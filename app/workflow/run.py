"""The Run model (CONTRACTS.md#run) and the one function that changes its state."""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.workflow.stale import StaleArtifacts, stale_names
from app.workflow.states import RunState, can_transition

SCHEMA_VERSION = 1
MAX_PROMPT_LENGTH = 10_000


class IllegalTransition(Exception):
    def __init__(self, from_: RunState, to: RunState):
        self.from_ = from_
        self.to = to
        super().__init__(f"{from_.value} → {to.value} is not allowed")


class HistoryEntry(BaseModel):
    from_: RunState | None = Field(default=None, alias="from")
    to: RunState
    at: str
    reason: str
    actor: str

    model_config = {"populate_by_name": True}


class ArtifactRef(BaseModel):
    path: str
    version: int
    hash: str
    status: str
    based_on: dict[str, str] = Field(default_factory=dict)
    updated_at: str


class SqlEndpoint(BaseModel):
    server: str
    database: str


class RunScope(BaseModel):
    """The Fabric workspace and Lakehouse a run reads from (set by T-103)."""

    workspace_id: str
    workspace_name: str | None = None
    lakehouse_id: str
    lakehouse_name: str
    schemas: list[str] = Field(default_factory=list)
    sql_endpoint: SqlEndpoint | None = None


class ActiveJob(BaseModel):
    """Set while a job operation runs (T-102). The last finished job stays until
    the next one starts; only one job runs per run at a time."""

    job_id: str
    operation: str
    job_instance_id: str | None = None
    status: str = "queued"  # queued | running | succeeded | failed | cancelled
    started_at: str
    finished_at: str | None = None
    error: dict[str, Any] | None = None  # the operation's error envelope, when failed
    reserved_by: dict[str, Any] | None = None  # {oid, upn}: who reserved it (T-105 FR-3)


class Run(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    prompt: str
    subject_area: str | None = None
    desired_outputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    state: RunState = RunState.CREATED
    resume_state: RunState | None = None
    scope: RunScope | None = None
    created_by: dict[str, Any] | None = None
    created_at: str
    updated_at: str
    history: list[HistoryEntry] = Field(default_factory=list)
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    deployment: dict[str, Any] | None = None
    active_job: ActiveJob | None = None

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_prompt(prompt: str) -> str:
    if prompt is None or not prompt.strip():
        raise ValueError("Prompt cannot be empty")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError(
            f"Prompt is too long ({len(prompt)} characters). "
            f"The limit is {MAX_PROMPT_LENGTH} characters."
        )
    return prompt


def new_run(
    prompt: str,
    subject_area: str | None = None,
    desired_outputs=(),
    constraints=(),
    created_by: dict[str, Any] | None = None,
) -> Run:
    """A new CREATED run. `created_by` is {oid, upn, name}; its upn is the actor."""
    prompt = validate_prompt(prompt)
    now = utc_now()
    actor = created_by["upn"] if created_by else "system"
    return Run(
        run_id=str(uuid.uuid4()),
        prompt=prompt,
        subject_area=subject_area,
        desired_outputs=list(desired_outputs),
        constraints=list(constraints),
        created_by=created_by,
        created_at=now,
        updated_at=now,
        history=[
            HistoryEntry(
                from_=None,
                to=RunState.CREATED,
                at=now,
                reason="Run created",
                actor=actor,
            )
        ],
    )


def apply_transition(
    run: Run, to: RunState | None, reason: str, actor: str = "system"
) -> Run:
    """Return a copy of `run` moved to `to`, or raise IllegalTransition.

    `to=None` is only valid from NEEDS_INPUT and means "return to the stored
    resume_state". The input run is never modified, so a rejected transition
    leaves state and history unchanged.
    """
    from_ = run.state
    if to is None:
        if from_ is not RunState.NEEDS_INPUT:
            raise ValueError("A target state is required unless the run is NEEDS_INPUT")
        to = run.resume_state or RunState.CREATED

    if to is RunState.DEPLOYING:  # before the legality check: a stale run is refused for that reason
        stale = stale_names(run.artifacts)
        if stale:
            raise StaleArtifacts("Deploying", stale)
    if not can_transition(from_, to, run.resume_state):
        raise IllegalTransition(from_, to)

    now = utc_now()
    updated = run.model_copy(deep=True)
    updated.state = to
    if to is RunState.NEEDS_INPUT:
        updated.resume_state = from_
    elif from_ is RunState.NEEDS_INPUT:
        updated.resume_state = None
    updated.updated_at = now
    updated.history.append(
        HistoryEntry(from_=from_, to=to, at=now, reason=reason, actor=actor)
    )
    return updated
