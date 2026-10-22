"""RunStore interface and its errors.

The store is the only code that persists run manifests. The run layer of the
hybrid orchestration (CONTRACTS.md#stage-graphs) goes through it.
"""

from typing import Protocol

from app.store.lease import LeaseBusy  # noqa: F401  (re-exported)
from app.workflow.run import IllegalTransition, Run  # noqa: F401  (re-exported)
from app.workflow.stale import StaleArtifacts  # noqa: F401  (re-exported)
from app.workflow.states import RunState


class RunNotFound(Exception):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Run not found: {run_id}")


class RunCorrupt(Exception):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Run manifest is corrupt: {run_id}")


class StoreUnavailable(Exception):
    """The backing store can't be reached or refused the request."""


class RunStore(Protocol):
    def create(
        self,
        prompt: str,
        subject_area: str | None = None,
        desired_outputs=(),
        constraints=(),
        created_by: dict | None = None,
    ) -> Run:
        """`created_by` is {oid, upn, name}; its upn becomes the history actor."""
        ...

    def get(self, run_id: str) -> Run:
        """Raises RunNotFound or RunCorrupt."""
        ...

    def list(self, state: RunState | None = None, limit: int = 50) -> list[Run]:
        """Newest first. Corrupt runs are skipped with a warning."""
        ...

    def save(self, run: Run) -> None: ...

    def transition(
        self,
        run_id: str,
        to: RunState | None,
        reason: str,
        actor: str = "system",
    ) -> Run:
        """Raises RunNotFound or IllegalTransition(from_, to).

        `to=None` leaves NEEDS_INPUT for the stored resume_state.
        """
        ...

    def lease(self, run_id: str, owner: str, seconds: int = 900) -> str:
        """Take the run's lease, so another writer can't interleave with `owner`.
        Returns a lease token. Raises RunNotFound or LeaseBusy (after waiting up
        to 30s for another owner's lease to free up or expire)."""
        ...

    def release(self, run_id: str, owner: str) -> None:
        """Release `owner`'s lease. A no-op if it isn't held, or held by someone else."""
        ...
