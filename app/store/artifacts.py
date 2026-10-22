"""Where every run file other than RunManifest.json lives (T-101 FR-3;
CONTRACTS.md#storage-layout). The store that writes manifests (`RunStore`) and
the store that writes every other run file (`ArtifactStore`) are separate, so
stage code never cares where it runs: it reads and writes through one of these
two interfaces, never `open()` on a path under `runs/<run_id>/` directly.
"""

import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Protocol

import requests

from app.config import PROJECT_ROOT
from app.connectors import onelake_files
from app.fsutil import replace_file
from app.store.lease import DEFAULT_WAIT_SECONDS, FileLease, LeaseBusy

logger = logging.getLogger(__name__)

APPEND_LEASE_SECONDS = 60


class NotFound(Exception):
    """No such file under this run's folder."""

    def __init__(self, run_id: str, rel: str):
        self.run_id = run_id
        self.rel = rel
        super().__init__(f"Not found: runs/{run_id}/{rel}")


def _check_rel(rel: str) -> Path:
    candidate = Path(rel) if rel else Path(".")
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Invalid relative path: {rel!r}")
    return candidate


class ArtifactStore(Protocol):
    def read_bytes(self, run_id: str, rel: str) -> bytes:
        """Raises NotFound."""
        ...

    def write_atomic(self, run_id: str, rel: str, data: bytes) -> None: ...

    def append_line(self, run_id: str, rel: str, line: str) -> None:
        """Appends one line (a trailing newline is added if missing), safe under
        concurrent writers: it takes the run's lease first."""
        ...

    def exists(self, run_id: str, rel: str) -> bool: ...

    def list(self, run_id: str, prefix: str) -> list[str]:
        """Every file path under `prefix` (relative to the run folder), recursively."""
        ...

    def copy_tree(self, run_id: str, src: "str | Path", dst: str) -> None:
        """Copy every file under local folder `src` into the run folder at `dst`.
        Used to commit a staging folder (TMDL, PBIR) built on local disk."""
        ...

    def local_path(self, run_id: str, rel: str) -> "Path | None":
        """A real filesystem path when one exists (local store, or a job's default-
        Lakehouse mount), for tools that need a folder. `None` otherwise: the caller
        stages in a temporary folder and uploads with `copy_tree`."""
        ...


class LocalArtifactStore:
    """The local `runs/<run_id>/` folder, as the first build used directly."""

    def __init__(self, root: Path = PROJECT_ROOT / "runs"):
        self.root = Path(root)
        self._lease = FileLease(self.root)

    def _path(self, run_id: str, rel: str) -> Path:
        candidate = _check_rel(rel)
        root = (self.root / run_id).resolve()
        target = (root / candidate).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"Invalid relative path: {rel!r}")
        return target

    def read_bytes(self, run_id: str, rel: str) -> bytes:
        try:
            return self._path(run_id, rel).read_bytes()
        except FileNotFoundError:
            raise NotFound(run_id, rel) from None

    def write_atomic(self, run_id: str, rel: str, data: bytes) -> None:
        path = self._path(run_id, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        replace_file(tmp, path)

    def append_line(self, run_id: str, rel: str, line: str) -> None:
        owner = f"append:{uuid.uuid4()}"
        self._lease.acquire(run_id, owner, seconds=APPEND_LEASE_SECONDS)
        try:
            path = self._path(run_id, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line.rstrip("\n") + "\n")
        finally:
            self._lease.release(run_id, owner)

    def exists(self, run_id: str, rel: str) -> bool:
        return self._path(run_id, rel).is_file()

    def list(self, run_id: str, prefix: str) -> list[str]:
        base = (self.root / run_id).resolve()
        start = self._path(run_id, prefix)
        if not start.exists():
            return []
        if start.is_file():
            return [str(start.relative_to(base)).replace("\\", "/")]
        return sorted(
            str(p.relative_to(base)).replace("\\", "/")
            for p in start.rglob("*")
            if p.is_file()
        )

    def copy_tree(self, run_id: str, src: "str | Path", dst: str) -> None:
        source = Path(src)
        if not source.is_absolute():
            source = self._path(run_id, str(src))
        dest_root = self._path(run_id, dst)
        if source.is_file():
            dest_root.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_root.with_name(dest_root.name + ".tmp")
            shutil.copy2(source, tmp)
            replace_file(tmp, dest_root)
            return
        for file in source.rglob("*"):
            if not file.is_file():
                continue
            target = dest_root / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            shutil.copy2(file, tmp)
            replace_file(tmp, target)

    def local_path(self, run_id: str, rel: str) -> Path | None:
        return self._path(run_id, rel)


class OneLakeArtifactStore:
    """`Files/runs/<run_id>/…` of the control Lakehouse `nexora_control`, through
    the OneLake DFS API. Not yet run against a live tenant (see fabric-hosting.md's
    platform-limits table); the local store above is what every offline check uses.
    """

    MOUNT = Path("/lakehouse/default/Files/runs")

    def __init__(
        self,
        workspace_id: str,
        lakehouse_id: str,
        token_provider=None,
    ):
        self.workspace_id = workspace_id
        self.lakehouse_id = lakehouse_id
        self._token_provider = token_provider

    def _rel(self, run_id: str, rel: str) -> str:
        candidate = _check_rel(rel)
        parts = [p for p in ("runs", run_id, str(candidate)) if p and p != "."]
        return "/".join(parts)

    def read_bytes(self, run_id: str, rel: str) -> bytes:
        try:
            return onelake_files.read_file(
                self.workspace_id, self.lakehouse_id, self._rel(run_id, rel), self._token_provider
            )
        except requests.HTTPError as error:
            if getattr(error.response, "status_code", None) == 404:
                raise NotFound(run_id, rel) from None
            raise

    def write_atomic(self, run_id: str, rel: str, data: bytes) -> None:
        # OneLake's create+append+flush already commits the file as one unit: a
        # reader never observes a partially-written flush.
        onelake_files.write_file(
            self.workspace_id, self.lakehouse_id, self._rel(run_id, rel), data, self._token_provider
        )

    def _lock_path(self, run_id: str) -> str:
        return self._rel(run_id, ".lock")

    def _acquire_lease(self, run_id: str, owner: str, seconds: int) -> None:
        lock_rel = self._lock_path(run_id)
        deadline = time.monotonic() + DEFAULT_WAIT_SECONDS
        while True:
            payload = json.dumps(
                {"owner": owner, "expires_at": time.time() + seconds}
            ).encode("utf-8")
            if onelake_files.create_if_absent(
                self.workspace_id, self.lakehouse_id, lock_rel, payload, self._token_provider
            ):
                return
            try:
                current = json.loads(
                    onelake_files.read_file(
                        self.workspace_id, self.lakehouse_id, lock_rel, self._token_provider
                    )
                )
            except (requests.HTTPError, ValueError):
                current = None
            if current is None or float(current.get("expires_at", 0)) < time.time():
                logger.warning(
                    "OneLake lease for run %s past its expiry; taking it over (new owner=%s)",
                    run_id,
                    owner,
                )
                onelake_files.delete_file(
                    self.workspace_id, self.lakehouse_id, lock_rel, self._token_provider
                )
                continue
            if time.monotonic() >= deadline:
                raise LeaseBusy(run_id, current.get("owner"))
            time.sleep(0.2)

    def _release_lease(self, run_id: str) -> None:
        onelake_files.delete_file(
            self.workspace_id, self.lakehouse_id, self._lock_path(run_id), self._token_provider
        )

    def append_line(self, run_id: str, rel: str, line: str) -> None:
        owner = f"append:{uuid.uuid4()}"
        self._acquire_lease(run_id, owner, seconds=APPEND_LEASE_SECONDS)
        try:
            try:
                existing = self.read_bytes(run_id, rel)
            except NotFound:
                existing = b""
            existing += (line.rstrip("\n") + "\n").encode("utf-8")
            self.write_atomic(run_id, rel, existing)
        finally:
            self._release_lease(run_id)

    def exists(self, run_id: str, rel: str) -> bool:
        try:
            self.read_bytes(run_id, rel)
            return True
        except NotFound:
            return False

    def list(self, run_id: str, prefix: str) -> list[str]:
        directory = self._rel(run_id, prefix)
        run_prefix = self._rel(run_id, "") + "/"
        return [p[len(run_prefix) :] for p in onelake_files.list_files(
            self.workspace_id, self.lakehouse_id, directory, self._token_provider
        )]

    def copy_tree(self, run_id: str, src: "str | Path", dst: str) -> None:
        source = Path(src)
        files = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()]
        for file in files:
            rel = "" if source.is_file() else str(file.relative_to(source)).replace("\\", "/")
            target_rel = f"{dst}/{rel}".strip("/") if rel else dst
            self.write_atomic(run_id, target_rel, file.read_bytes())

    def local_path(self, run_id: str, rel: str) -> Path | None:
        """The default-Lakehouse mount, when this process is a job whose default
        Lakehouse is the control Lakehouse (fabric-hosting.md's storage layout).
        `None` otherwise, so the caller stages in a temp folder and calls `copy_tree`.
        """
        if not self.MOUNT.is_dir():
            return None
        candidate = _check_rel(rel)
        return (self.MOUNT / run_id / candidate).resolve()
