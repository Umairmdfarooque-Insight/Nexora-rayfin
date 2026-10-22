"""File-backed RunStore: runs/<run_id>/RunManifest.json."""

import logging
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.fsutil import replace_file
from app.store.base import RunCorrupt, RunNotFound
from app.store.lease import DEFAULT_LEASE_SECONDS, FileLease
from app.workflow.run import Run, apply_transition, new_run
from app.workflow.states import RunState

logger = logging.getLogger(__name__)

MANIFEST_NAME = "RunManifest.json"


class FileRunStore:
    def __init__(self, root: Path = PROJECT_ROOT / "runs"):
        self.root = Path(root)
        self._lease = FileLease(self.root)

    def _manifest_path(self, run_id: str) -> Path:
        return self.root / run_id / MANIFEST_NAME

    @staticmethod
    def _is_valid_id(run_id: str) -> bool:
        # Run IDs are UUIDs; rejecting anything else also blocks path tricks.
        try:
            uuid.UUID(run_id)
        except (ValueError, AttributeError, TypeError):
            return False
        return True

    def create(
        self,
        prompt,
        subject_area=None,
        desired_outputs=(),
        constraints=(),
        created_by=None,
    ) -> Run:
        run = new_run(prompt, subject_area, desired_outputs, constraints, created_by)
        self.save(run)
        return run

    def get(self, run_id: str) -> Run:
        if not self._is_valid_id(run_id):
            raise RunNotFound(run_id)
        path = self._manifest_path(run_id)
        if not path.is_file():
            raise RunNotFound(run_id)
        try:
            return Run.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            raise RunCorrupt(run_id) from None

    def list(self, state: RunState | None = None, limit: int = 50) -> list[Run]:
        if not self.root.is_dir():
            return []
        runs: list[Run] = []
        for entry in self.root.iterdir():
            if not entry.is_dir() or not self._is_valid_id(entry.name):
                continue
            try:
                run = self.get(entry.name)
            except RunNotFound:
                continue
            except RunCorrupt:
                logger.warning("Skipping corrupt run manifest: %s", entry.name)
                continue
            if state is None or run.state == state:
                runs.append(run)
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def save(self, run: Run) -> None:
        path = self._manifest_path(run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(MANIFEST_NAME + ".tmp")
        tmp.write_text(run.to_json(), encoding="utf-8")
        replace_file(tmp, path)

    def transition(
        self, run_id: str, to: RunState | None, reason: str, actor: str = "system"
    ) -> Run:
        run = apply_transition(self.get(run_id), to, reason, actor)
        self.save(run)
        return run

    def lease(self, run_id: str, owner: str, seconds: int = DEFAULT_LEASE_SECONDS) -> str:
        self.get(run_id)  # raises RunNotFound for an unknown run
        return self._lease.acquire(run_id, owner, seconds=seconds)

    def release(self, run_id: str, owner: str) -> None:
        self._lease.release(run_id, owner)
