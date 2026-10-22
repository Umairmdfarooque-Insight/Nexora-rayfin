"""Run authorization (T-705, NFR-002): who may read or act on a run.

A user may if they created it, or if they can access the workspace the run is scoped to. Access to the
workspace is read from Fabric, as the user (their own workspace list), and cached for 5 minutes, so a
user removed from a workspace loses its runs within 5 minutes. The cache is keyed by tenant, workspace
and user, so nothing one scope learned is used for another.

A run with no `created_by` was created through the CLI, which has no signed-in user; it has no owner to
protect and stays open to any signed-in user. A run with an owner and no scope is the owner's alone.

**Fabric rework (T-705).** A UDF has no user-delegated Fabric token at all (fabric-hosting.md), so it
can't call `list_workspaces` itself; only a job can. The decision therefore has to be made in a job and
then read back by a UDF, which means an in-process cache is useless (a UDF instance is typically a fresh
process per call). `default_cache()` picks a SQL-backed cache (`dbo.nexora_run_access`, this control
database) when `RUN_STORE=fabric_sql`, so the decision a job makes is visible to the next UDF call; the
in-memory `AccessCache` below stays the default for local development (`RUN_STORE=file`), where a UDF and
a job are just the same process. `app.fabric.dispatch._authorize` is what actually enforces this: no
fresh row and the caller isn't a job gets `403 ACCESS_CHECK_REQUIRED`, which the UI resolves by running
`job:check_access` (`POST /runs/{id}/check_access`) and retrying.
"""

import time
from collections.abc import Callable
from typing import Any

from app.config import ENTRA_TENANT_ID
from app.workflow.run import Run

TTL_SECONDS = 5 * 60


class RunAccessDenied(Exception):
    """The signed-in user may not read or act on this run."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__("You don't have access to this run: it was created by someone else and you can't access its workspace")


class AccessCache:
    def __init__(self, ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._items: dict[tuple, tuple[float, bool]] = {}

    def get(self, key: tuple) -> bool | None:
        found = self._items.get(key)
        if found is None or self.clock() - found[0] >= self.ttl:
            self._items.pop(key, None)
            return None
        return found[1]

    def set(self, key: tuple, value: bool) -> None:
        self._items[key] = (self.clock(), value)

    def clear(self) -> None:
        self._items.clear()


cache = AccessCache()


class SqlAccessCache:
    """The same `get`/`set`/`clear` interface as `AccessCache`, backed by `dbo.nexora_run_access` (T-705)
    so the decision survives across UDF and job instances. `checked_at` (seconds since the epoch) is what
    `ttl` is checked against, same as `AccessCache`'s in-memory timestamp."""

    TABLE = "dbo.nexora_run_access"
    CREATE_TABLE_SQL = f"""
IF OBJECT_ID(N'{TABLE}', N'U') IS NULL
CREATE TABLE {TABLE} (
    tenant_id    NVARCHAR(64) NOT NULL,
    workspace_id NVARCHAR(64) NOT NULL,
    user_oid     NVARCHAR(64) NOT NULL,
    allowed      BIT          NOT NULL,
    checked_at   FLOAT        NOT NULL,
    CONSTRAINT PK_nexora_run_access PRIMARY KEY (tenant_id, workspace_id, user_oid)
)
"""

    def __init__(
        self,
        server: str,
        database: str,
        ttl: float = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        connect: "Callable[[], Any] | None" = None,
    ):
        self.ttl, self.clock = ttl, clock
        self._server, self._database = server, database
        self._connect_fn = connect
        self._schema_ready = False

    def _connect(self) -> Any:
        if self._connect_fn is not None:
            return self._connect_fn()
        from app.connectors.fabric_sql_connector import FabricSQLConnector

        return FabricSQLConnector(self._server, self._database).connect()

    def _ensure_schema(self, connection: Any) -> None:
        if not self._schema_ready:
            connection.cursor().execute(self.CREATE_TABLE_SQL)
            connection.commit()
            self._schema_ready = True

    def get(self, key: tuple) -> bool | None:
        tenant, workspace, oid = key
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT allowed, checked_at FROM {self.TABLE} WHERE tenant_id = ? AND workspace_id = ? AND user_oid = ?",
                tenant, workspace, oid,
            )
            row = cursor.fetchone()
            if row is None or self.clock() - float(row[1]) >= self.ttl:
                return None
            return bool(row[0])
        finally:
            connection.close()

    def set(self, key: tuple, value: bool) -> None:
        tenant, workspace, oid = key
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            now = self.clock()
            cursor = connection.cursor()
            cursor.execute(
                f"UPDATE {self.TABLE} SET allowed = ?, checked_at = ? WHERE tenant_id = ? AND workspace_id = ? AND user_oid = ?",
                value, now, tenant, workspace, oid,
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    f"INSERT INTO {self.TABLE} (tenant_id, workspace_id, user_oid, allowed, checked_at) VALUES (?, ?, ?, ?, ?)",
                    tenant, workspace, oid, value, now,
                )
            connection.commit()
        finally:
            connection.close()

    def clear(self) -> None:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.cursor().execute(f"DELETE FROM {self.TABLE}")
            connection.commit()
        finally:
            connection.close()


_sql_cache: "SqlAccessCache | None" = None


def default_cache() -> "AccessCache | SqlAccessCache":
    """The in-memory `cache` for local development (`RUN_STORE=file`, where a UDF and a job are the same
    process anyway), or the SQL-backed one when `RUN_STORE=fabric_sql`."""
    global _sql_cache
    from app.config import get_optional_setting, get_required_setting

    kind = (get_optional_setting("RUN_STORE", "file") or "file").lower()
    if kind != "fabric_sql":
        return cache
    if _sql_cache is None:
        _sql_cache = SqlAccessCache(get_required_setting("FABRIC_CONTROL_DB_SERVER"), get_required_setting("FABRIC_CONTROL_DB_NAME"))
    return _sql_cache


def _key(run: Run, oid: str) -> tuple[str, str, str] | None:
    if run.scope is None:
        return None
    return (ENTRA_TENANT_ID or "").casefold(), run.scope.workspace_id.casefold(), oid


def cached_decision(run: Run, oid: str, access: "AccessCache | SqlAccessCache | None" = None) -> bool | None:
    """The cached decision for this run's scope and this user, or `None` if there is no fresh one yet.
    Never calls Fabric: a UDF has no user-delegated token to do that with (T-705) -- only a job's own
    `can_access` call may list workspaces and write a fresh decision. `app.fabric.dispatch._authorize`
    is what calls this for a UDF caller who isn't the run's owner."""
    key = _key(run, oid)
    if key is None:
        return None
    return (access or default_cache()).get(key)


def can_access(
    run: Run, oid: str, upn: str, list_workspaces: Callable[[], list[dict[str, Any]]], access: "AccessCache | SqlAccessCache | None" = None
) -> bool:
    """True if the user created the run, or the run has no owner, or Fabric lists its workspace for them.
    A workspace list that can't be read is a refusal (and isn't cached), never a pass. Called only where a
    user-delegated Fabric token exists (a job, or local development): a UDF uses `cached_decision` instead,
    since it can't call `list_workspaces` itself."""
    owner = run.created_by
    if owner is None:
        return True
    if owner.get("oid") == oid or str(owner.get("upn", "")).casefold() == upn.casefold():
        return True
    key = _key(run, oid)
    if key is None:
        return False
    store = access or default_cache()
    known = store.get(key)
    if known is not None:
        return known
    try:
        workspace = key[1]
        allowed = any(str(w.get("id", "")).casefold() == workspace for w in list_workspaces())
    except Exception:
        return False
    store.set(key, allowed)
    return allowed
