"""The promotion target lock (T-702 FR-2, FR-8): one deployment per target workspace at a time.

`dbo.nexora_locks` (PK `target_workspace_id`; CONTRACTS.md#M7) backs the lock when `RUN_STORE=fabric_sql`
(the same control database as `dbo.nexora_runs`, created on first use the same way `SqlRunStore` creates
its own table). The local file lock (`<runs_root>/.locks/<workspace>.lock`) backs it for local development
(`RUN_STORE=file`), so a deployment can be exercised end to end without a SQL Database. A lock older than
`LOCK_MAX_AGE` was left by a crashed or cancelled job: it is replaced and logged, never left to block every
future deployment to that target.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import get_optional_setting, get_required_setting

logger = logging.getLogger(__name__)

LOCK_DIR = ".locks"
LOCK_MAX_AGE = 2 * 60 * 60
TABLE = "dbo.nexora_locks"

CREATE_TABLE_SQL = f"""
IF OBJECT_ID(N'{TABLE}', N'U') IS NULL
CREATE TABLE {TABLE} (
    target_workspace_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    run_id               NVARCHAR(36) NOT NULL,
    deployment_id        NVARCHAR(36) NOT NULL,
    taken_at             FLOAT        NOT NULL
)
"""


class TargetBusy(Exception):
    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        super().__init__(f"Target busy: another deployment to workspace {workspace_id} is running")


@dataclass
class LockHandle:
    workspace_id: str
    backend: str
    path: Path | None = None


def _kind() -> str:
    return (get_optional_setting("RUN_STORE", "file") or "file").lower()


# --- file backend (local development) ---------------------------------------------------------------


def _safe_name(workspace_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "_", workspace_id.casefold())


def _file_acquire(runs_root: Path, workspace_id: str, run_id: str, deployment_id: str, owner: str) -> LockHandle:
    folder = Path(runs_root) / LOCK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(workspace_id)}.lock"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - path.stat().st_mtime > LOCK_MAX_AGE:
                logger.warning("Target lock for workspace %s past its max age (file); taking it over", workspace_id)
                path.unlink(missing_ok=True)
                continue
            raise TargetBusy(workspace_id) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"owner": owner, "run_id": run_id, "deployment_id": deployment_id, "taken_at": time.time()}))
        return LockHandle(workspace_id, "file", path=path)
    raise TargetBusy(workspace_id)


def _file_release(handle: LockHandle) -> None:
    if handle.path is not None:
        handle.path.unlink(missing_ok=True)


# --- SQL backend (dbo.nexora_locks) -----------------------------------------------------------------


def _sql_connect():
    from app.connectors.fabric_sql_connector import FabricSQLConnector

    server = get_required_setting("FABRIC_CONTROL_DB_SERVER")
    database = get_required_setting("FABRIC_CONTROL_DB_NAME")
    return FabricSQLConnector(server, database).connect()


def _sql_acquire(workspace_id: str, run_id: str, deployment_id: str) -> LockHandle:
    import pyodbc

    from app.store.base import StoreUnavailable

    try:
        connection = _sql_connect()
    except pyodbc.Error as exc:
        raise StoreUnavailable(f"Cannot reach the control database for the target lock: {exc}") from exc
    try:
        cursor = connection.cursor()
        cursor.execute(CREATE_TABLE_SQL)
        connection.commit()
        now = time.time()
        try:
            cursor.execute(
                f"INSERT INTO {TABLE} (target_workspace_id, run_id, deployment_id, taken_at) VALUES (?, ?, ?, ?)",
                workspace_id,
                run_id,
                deployment_id,
                now,
            )
            connection.commit()
            return LockHandle(workspace_id, "sql")
        except pyodbc.Error:
            connection.rollback()
            cursor.execute(f"SELECT taken_at FROM {TABLE} WHERE target_workspace_id = ?", workspace_id)
            row = cursor.fetchone()
            if row is not None and now - float(row[0]) > LOCK_MAX_AGE:
                logger.warning("Target lock for workspace %s past its max age (sql); taking it over", workspace_id)
                cursor.execute(
                    f"UPDATE {TABLE} SET run_id = ?, deployment_id = ?, taken_at = ? "
                    "WHERE target_workspace_id = ? AND taken_at < ?",
                    run_id,
                    deployment_id,
                    now,
                    workspace_id,
                    now - LOCK_MAX_AGE,
                )
                connection.commit()
                if cursor.rowcount == 1:
                    return LockHandle(workspace_id, "sql")
            raise TargetBusy(workspace_id) from None
    finally:
        connection.close()


def _sql_release(handle: LockHandle) -> None:
    connection = _sql_connect()
    try:
        cursor = connection.cursor()
        cursor.execute(f"DELETE FROM {TABLE} WHERE target_workspace_id = ?", handle.workspace_id)
        connection.commit()
    finally:
        connection.close()


# --- public API ----------------------------------------------------------------------------------------


def acquire(runs_root: Path, workspace_id: str, run_id: str, deployment_id: str, owner: str) -> LockHandle:
    """One deployment per target workspace at a time. Raises TargetBusy (or StoreUnavailable if the
    control database can't be reached, when `RUN_STORE=fabric_sql`)."""
    if _kind() == "fabric_sql":
        return _sql_acquire(workspace_id, run_id, deployment_id)
    return _file_acquire(runs_root, workspace_id, run_id, deployment_id, owner)


def release(handle: LockHandle) -> None:
    if handle.backend == "sql":
        _sql_release(handle)
    else:
        _file_release(handle)


def release_by_workspace(runs_root: Path, workspace_id: str) -> None:
    """Release a lock this process didn't itself acquire: reconstructs the same handle `acquire` would
    have returned, from the workspace id alone. Used to resume releasing a lock after a deployment job
    was cancelled or crashed (T-702 FR-3), in a process that never held the original `LockHandle`."""
    if _kind() == "fabric_sql":
        _sql_release(LockHandle(workspace_id, "sql"))
    else:
        path = Path(runs_root) / LOCK_DIR / f"{_safe_name(workspace_id)}.lock"
        _file_release(LockHandle(workspace_id, "file", path=path))
