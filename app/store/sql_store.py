"""Fabric SQL Database RunStore.

Keeps one row per run in `nexora_runs`. The full manifest is stored as JSON,
with `state` and `created_at` copied into columns for filtering and ordering.
This must point at a writable Fabric *SQL Database*, not the read-only
Lakehouse SQL endpoint.
"""

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Callable

import pyodbc
from pydantic import ValidationError

from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.store.base import RunCorrupt, RunNotFound, StoreUnavailable
from app.store.lease import DEFAULT_LEASE_SECONDS, DEFAULT_WAIT_SECONDS, LeaseBusy
from app.workflow.run import Run, apply_transition, new_run
from app.workflow.states import RunState

logger = logging.getLogger(__name__)

TABLE = "dbo.nexora_runs"
MAX_TRANSITION_ATTEMPTS = 3
LEASE_POLL_SECONDS = 0.2

CREATE_TABLE_SQL = f"""
IF OBJECT_ID(N'{TABLE}', N'U') IS NULL
CREATE TABLE {TABLE} (
    run_id           NVARCHAR(36)  NOT NULL PRIMARY KEY,
    state            NVARCHAR(32)  NOT NULL,
    created_at       NVARCHAR(20)  NOT NULL,
    updated_at       NVARCHAR(20)  NOT NULL,
    version          INT           NOT NULL,
    manifest         NVARCHAR(MAX) NOT NULL,
    active_job       NVARCHAR(MAX) NULL,
    lease_owner      NVARCHAR(256) NULL,
    lease_expires_at FLOAT         NULL
)
"""

# Added after the table already existed in an earlier deployment (T-101, T-102).
MIGRATE_COLUMNS_SQL = [
    f"IF COL_LENGTH('{TABLE}', 'active_job') IS NULL ALTER TABLE {TABLE} ADD active_job NVARCHAR(MAX) NULL",
    f"IF COL_LENGTH('{TABLE}', 'lease_owner') IS NULL ALTER TABLE {TABLE} ADD lease_owner NVARCHAR(256) NULL",
    f"IF COL_LENGTH('{TABLE}', 'lease_expires_at') IS NULL ALTER TABLE {TABLE} ADD lease_expires_at FLOAT NULL",
]


class SqlRunStore:
    def __init__(
        self,
        server: str,
        database: str,
        connect: Callable[[], "pyodbc.Connection"] | None = None,
    ):
        self.server = server
        self.database = database
        self._connect = connect or FabricSQLConnector(server, database).connect
        self._schema_ready = False

    @contextmanager
    def _cursor(self):
        """One transaction: commit on success, roll back on error, always close."""
        try:
            connection = self._connect()
        except pyodbc.Error as exc:
            raise StoreUnavailable(
                f"Cannot connect to control database {self.database} on "
                f"{self.server}: {exc}"
            ) from exc
        try:
            if not self._schema_ready:
                connection.cursor().execute(CREATE_TABLE_SQL)
                for statement in MIGRATE_COLUMNS_SQL:
                    connection.cursor().execute(statement)
                self._schema_ready = True
            cursor = connection.cursor()
            yield cursor
            connection.commit()
        except pyodbc.Error as exc:
            connection.rollback()
            raise StoreUnavailable(f"Control database request failed: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _parse(run_id: str, manifest: str) -> Run:
        try:
            return Run.model_validate_json(manifest)
        except (ValidationError, ValueError):
            raise RunCorrupt(run_id) from None

    def _read(self, cursor, run_id: str) -> tuple[Run, int]:
        cursor.execute(
            f"SELECT manifest, version FROM {TABLE} WHERE run_id = ?", run_id
        )
        row = cursor.fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._parse(run_id, row[0]), row[1]

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
        with self._cursor() as cursor:
            return self._read(cursor, run_id)[0]

    def list(self, state: RunState | None = None, limit: int = 50) -> list[Run]:
        sql = f"SELECT TOP (?) run_id, manifest FROM {TABLE} "
        params: list = [limit]
        if state is not None:
            sql += "WHERE state = ? "
            params.append(state.value)
        sql += "ORDER BY created_at DESC"
        with self._cursor() as cursor:
            cursor.execute(sql, *params)
            rows = cursor.fetchall()
        runs = []
        for run_id, manifest in rows:
            try:
                runs.append(self._parse(run_id, manifest))
            except RunCorrupt:
                logger.warning("Skipping corrupt run manifest: %s", run_id)
        return runs

    def save(self, run: Run) -> None:
        active_job_json = run.active_job.model_dump_json() if run.active_job else None
        values = (run.state.value, run.updated_at, run.to_json(), active_job_json, run.run_id)
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET state = ?, updated_at = ?, manifest = ?, active_job = ?, "
                "version = version + 1 WHERE run_id = ?",
                *values,
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    f"INSERT INTO {TABLE} "
                    "(run_id, state, created_at, updated_at, version, manifest) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    run.run_id,
                    run.state.value,
                    run.created_at,
                    run.updated_at,
                    run.to_json(),
                )

    def transition(
        self, run_id: str, to: RunState | None, reason: str, actor: str = "system"
    ) -> Run:
        # Optimistic concurrency: if another writer changed the row between
        # our read and write, re-read and re-validate against the new state.
        for _ in range(MAX_TRANSITION_ATTEMPTS):
            with self._cursor() as cursor:
                run, version = self._read(cursor, run_id)
                updated = apply_transition(run, to, reason, actor)
                cursor.execute(
                    f"UPDATE {TABLE} SET state = ?, updated_at = ?, manifest = ?, "
                    "version = version + 1 WHERE run_id = ? AND version = ?",
                    updated.state.value,
                    updated.updated_at,
                    updated.to_json(),
                    run_id,
                    version,
                )
                if cursor.rowcount == 1:
                    return updated
        raise StoreUnavailable(
            f"Run {run_id} is being changed by another writer; try again"
        )

    def lease(self, run_id: str, owner: str, seconds: int = DEFAULT_LEASE_SECONDS) -> str:
        token = str(uuid.uuid4())
        deadline = time.monotonic() + DEFAULT_WAIT_SECONDS
        while True:
            now = time.time()
            with self._cursor() as cursor:
                cursor.execute(
                    f"SELECT lease_owner, lease_expires_at FROM {TABLE} WHERE run_id = ?",
                    run_id,
                )
                row = cursor.fetchone()
                if row is None:
                    raise RunNotFound(run_id)
                holder, expires_at = row
                free = holder is None or expires_at is None or float(expires_at) < now
                if free:
                    cursor.execute(
                        f"UPDATE {TABLE} SET lease_owner = ?, lease_expires_at = ? "
                        "WHERE run_id = ? AND (lease_owner IS NULL OR lease_expires_at < ?)",
                        owner,
                        now + seconds,
                        run_id,
                        now,
                    )
                    if cursor.rowcount == 1:
                        if holder is not None:
                            logger.warning(
                                "Lease for run %s past its expiry; taking it over (new owner=%s)",
                                run_id,
                                owner,
                            )
                        return token
            if time.monotonic() >= deadline:
                raise LeaseBusy(run_id, holder)
            time.sleep(LEASE_POLL_SECONDS)

    def release(self, run_id: str, owner: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET lease_owner = NULL, lease_expires_at = NULL "
                "WHERE run_id = ? AND lease_owner = ?",
                run_id,
                owner,
            )
