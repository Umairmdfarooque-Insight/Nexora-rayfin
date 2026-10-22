"""Run-level lease (T-101 FR-6): writers of a run's files take it first, so a UDF
edit and a running job (or two concurrent writers of any kind) never interleave.

`FileLease` is the local, file-backed implementation shared by `FileRunStore`
(long, 900s-default business leases held across a whole job) and
`LocalArtifactStore` (short, per-call leases that just serialize one
`append_line`/`write_atomic`). Both point at the same `<root>/.locks/<run_id>.lock`
file, so they coordinate with each other.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900
DEFAULT_WAIT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.2
RELEASE_ATTEMPTS = 8
RELEASE_RETRY_WAIT = 0.05


class LeaseBusy(Exception):
    """The lease is held by another owner and didn't free up within the wait."""

    def __init__(self, run_id: str, owner: str | None):
        self.run_id = run_id
        self.holder = owner
        super().__init__(f"Run {run_id} is locked by another writer ({owner})")


class FileLease:
    """A lease backed by a lock file: `<root>/.locks/<run_id>.lock`, holding JSON
    `{"owner", "token", "expires_at"}`. Acquisition is atomic (exclusive file
    creation); a lease past its expiry is taken over and logged.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        directory = self.root / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{run_id}.lock"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def acquire(
        self,
        run_id: str,
        owner: str,
        seconds: int = DEFAULT_LEASE_SECONDS,
        wait: float = DEFAULT_WAIT_SECONDS,
    ) -> str:
        """Return a lease token once `owner` holds the lease. Raises `LeaseBusy`
        after waiting `wait` seconds for another owner's lease to free up."""
        path = self._path(run_id)
        token = str(uuid.uuid4())
        deadline = time.monotonic() + wait
        while True:
            payload = json.dumps(
                {"owner": owner, "token": token, "expires_at": time.time() + seconds}
            ).encode("utf-8")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                return token
            except (FileExistsError, PermissionError):
                # On Windows, a concurrent create can raise PermissionError (not
                # FileExistsError) while another thread's release()/takeover unlink
                # is still in flight; treat it exactly like contention, not a failure.
                current = self._read(path)
                if current is None:
                    continue  # released concurrently between our EEXIST and this read
                if float(current.get("expires_at", 0)) < time.time():
                    logger.warning(
                        "Lease for run %s past its expiry; taking it over (new owner=%s)",
                        run_id,
                        owner,
                    )
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LeaseBusy(run_id, current.get("owner"))
                time.sleep(POLL_INTERVAL_SECONDS)

    def release(self, run_id: str, owner: str) -> None:
        path = self._path(run_id)
        current = self._read(path)
        if current is None or current.get("owner") != owner:
            return  # already gone, or held by someone else: never release another owner's lease
        # On Windows a virus scanner or indexer can hold the file just written for a
        # moment, which fails the unlink with PermissionError (the same class of
        # transient failure app/fsutil.py:replace_file retries for renames). Left
        # unretried, a failed release here strands the lease until it expires,
        # blocking every other writer for up to `seconds` instead of microseconds.
        for attempt in range(RELEASE_ATTEMPTS):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == RELEASE_ATTEMPTS - 1:
                    logger.warning("Could not release lease for run %s (owner=%s)", run_id, owner)
                    return
                time.sleep(RELEASE_RETRY_WAIT * (attempt + 1))
