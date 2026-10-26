"""T-101 offline checks: state table, file store edge cases, SQL store logic.

Run: python -m app.manual_tests.t101_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The SQL store runs against an in-memory SQLite stand-in for the Fabric SQL
Database, so this checks the store's logic, not live Fabric access.
"""

import sqlite3
import tempfile
from pathlib import Path

from app.store.base import IllegalTransition, RunCorrupt, RunNotFound
from app.store.file_store import FileRunStore
from app.store.sql_store import CREATE_TABLE_SQL, SqlRunStore
from app.workflow.states import ALLOWED, RunState, can_transition

S = RunState


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    return None


class FakeSqlConnection:
    """Wraps SQLite so SqlRunStore's T-SQL runs unchanged for the test."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.cur = None
        self.rowcount = 0

    def execute(self, sql, *params):
        if "OBJECT_ID" in sql:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS nexora_runs (run_id TEXT PRIMARY KEY, "
                "state TEXT, created_at TEXT, updated_at TEXT, version INT, manifest TEXT, "
                "active_job TEXT, lease_owner TEXT, lease_expires_at REAL)"
            )
            return
        if "COL_LENGTH" in sql:
            return  # migration no-op: the fake CREATE TABLE above already has these columns
        sql = sql.replace("dbo.", "")
        if "SELECT TOP (?)" in sql:
            sql = sql.replace("SELECT TOP (?)", "SELECT") + " LIMIT ?"
            params = params[1:] + params[:1]
        self.cur = self.db.execute(sql, params)
        self.rowcount = self.cur.rowcount

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()


def main() -> None:
    # --- State table ---
    check("15 states", len(RunState) == 15)
    check("COMPLETED is terminal", ALLOWED[S.COMPLETED] == set())
    check(
        "every non-COMPLETED state can go to NEEDS_INPUT",
        all(S.NEEDS_INPUT in ALLOWED[s] for s in S if s not in (S.COMPLETED, S.NEEDS_INPUT)),
    )
    check("same-state is illegal", not any(can_transition(s, s) for s in S))
    check("CREATED -> DEPLOYING illegal", not can_transition(S.CREATED, S.DEPLOYING))

    # --- File store ---
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"  # missing until first write
        store = FileRunStore(root)
        check("list on missing folder is empty", store.list() == [])
        run = store.create("Total sales by month", desired_outputs=["report"])
        check("created state", run.state == S.CREATED and len(run.history) == 1)
        check("manifest written", (root / run.run_id / "RunManifest.json").is_file())
        check("no tmp file left", not (root / run.run_id / "RunManifest.json.tmp").exists())
        check("unique ids", store.create("Total sales by month").run_id != run.run_id)

        err = raises(IllegalTransition, store.transition, run.run_id, S.DEPLOYING, "x")
        check("illegal blocked with message", err and str(err) == "CREATED → DEPLOYING is not allowed")
        after = store.get(run.run_id)
        check("illegal leaves state and history", after.state == S.CREATED and len(after.history) == 1)
        check(
            "same-state illegal, no duplicate history",
            raises(IllegalTransition, store.transition, run.run_id, S.CREATED, "x")
            and len(store.get(run.run_id).history) == 1,
        )

        store.transition(run.run_id, S.NEEDS_CLARIFICATION, "test")
        h = store.get(run.run_id).history
        check("legal recorded with reason", len(h) == 2 and h[1].reason == "test" and h[1].at.endswith("Z"))
        store.transition(run.run_id, S.NEEDS_INPUT, "wait")
        check("resume_state stored", store.get(run.run_id).resume_state == S.NEEDS_CLARIFICATION)
        check(
            "NEEDS_INPUT cannot jump to another state",
            raises(IllegalTransition, store.transition, run.run_id, S.DATA_PROFILED, "x") is not None,
        )
        back = store.transition(run.run_id, None, "resume")
        check("resume returns to stored state", back.state == S.NEEDS_CLARIFICATION and back.resume_state is None)

        check("new instance sees same data", FileRunStore(root).get(run.run_id).state == S.NEEDS_CLARIFICATION)
        check("filter by state", [r.run_id for r in store.list(S.NEEDS_CLARIFICATION)] == [run.run_id])
        check("newest first", store.list()[0].created_at >= store.list()[-1].created_at)

        check("unknown id", raises(RunNotFound, store.get, "00000000-0000-4000-8000-000000000000") is not None)
        check("non-uuid id is not found", raises(RunNotFound, store.get, "../../x") is not None)
        check("empty prompt", str(raises(ValueError, store.create, "   ")) == "Prompt cannot be empty")
        check("long prompt", "10000" in str(raises(ValueError, store.create, "a" * 10001)))

        (root / run.run_id / "RunManifest.json").write_text("{ not json", encoding="utf-8")
        check("corrupt get", raises(RunCorrupt, store.get, run.run_id) is not None)
        check("corrupt skipped in list", all(r.run_id != run.run_id for r in store.list()))

    # --- SQL store (SQLite stand-in) ---
    db = sqlite3.connect(":memory:")
    sql_store = SqlRunStore("server", "db", connect=lambda: FakeSqlConnection(db))
    check("DDL targets nexora_runs", "nexora_runs" in CREATE_TABLE_SQL)
    run = sql_store.create("Total sales by month")
    check("sql create + get", sql_store.get(run.run_id).state == S.CREATED)
    check(
        "sql illegal blocked",
        raises(IllegalTransition, sql_store.transition, run.run_id, S.DEPLOYING, "x") is not None
        and sql_store.get(run.run_id).state == S.CREATED,
    )
    sql_store.transition(run.run_id, S.NEEDS_CLARIFICATION, "test")
    sql_store.transition(run.run_id, S.NEEDS_INPUT, "wait")
    check("sql resume_state", sql_store.get(run.run_id).resume_state == S.NEEDS_CLARIFICATION)
    check("sql resume", sql_store.transition(run.run_id, None, "go").state == S.NEEDS_CLARIFICATION)
    check("sql history", len(sql_store.get(run.run_id).history) == 4)
    check("sql unknown id", raises(RunNotFound, sql_store.get, "nope") is not None)
    check("sql filter", len(sql_store.list(S.CREATED)) == 0 and len(sql_store.list(S.NEEDS_CLARIFICATION)) == 1)
    db.execute("UPDATE nexora_runs SET manifest = 'garbage'")
    db.commit()
    check("sql corrupt get", raises(RunCorrupt, sql_store.get, run.run_id) is not None)
    check("sql corrupt skipped", sql_store.list() == [])

    print("All checks passed.")


if __name__ == "__main__":
    main()
