"""T-302 offline checks: the generic data profiler and POST /runs/{id}/profile.

Run: python -m app.manual_tests.t302_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The SQL endpoint is replaced by a fake that evaluates the profiler's SELECTs against
in-memory tables, so no sign-in or network is needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.discovery.data_profiler import classify_error, column_kind, quote_identifier
from app.store.file_store import FileRunStore
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

WS = "11111111-1111-4111-8111-111111111111"
LH = "22222222-2222-4222-8222-222222222222"
RANDOM = "99999999-9999-4999-8999-999999999999"

SECRET_NAME = "Mountain-500 Silver, 52"  # a string value that must never reach the inventory


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def col(name, type_name, nullable=True):
    return {"name": name, "type_name": type_name, "nullable": nullable}


# --- fake Lakehouse (for the harvest that creates the inventory) -------------------------
LAKEHOUSE = {
    "saleslt": {
        "SalesLT_Product": [
            col("ProductID", "INT", False), col("Name", "STRING"), col("Weight", "DECIMAL(8,2)"),
            col("ModifiedDate", "TIMESTAMP"), col("Thumbnail", "BINARY"),
        ],
        "SalesLT_Customer": [col("CustomerID", "INT", False), col("CompanyName", "STRING")],
        "Extra": [col("Id", "INT")],
    },
    "nexora_test": {
        "dim_product_dupes": [col("ProductID", "INT"), col("Name", "STRING"), col("Category", "STRING")],
        "dim_empty": [col("EmptyID", "INT"), col("Label", "STRING")],
        "dim_nulls": [col("NullKey", "INT"), col("Note", "STRING")],
        "dim_gone": [col("GoneID", "INT")],
        "dim_slow": [col("SlowID", "INT")],
        "dim_big": [col("BigID", "LONG")],
    },
}


class FakeLakehouse:
    def list_schemas(self):
        return [{"name": n} for n in LAKEHOUSE]

    def list_tables(self, schema):
        return [{"name": t} for t in LAKEHOUSE[schema]]

    def get_table_metadata(self, schema, table):
        return {"columns": LAKEHOUSE[schema][table]}


# --- fake SQL endpoint ------------------------------------------------------------------
D = datetime
DATA = {
    ("saleslt", "SalesLT_Product"): {
        "ProductID": [680, 706, 707, 708, 709, 710],
        "Name": [SECRET_NAME, "HL Road Frame", "Sport-100", "Sport-100", "Long-Sleeve", "Mountain Bike"],
        "Weight": [Decimal("1.50"), None, Decimal("2.25"), None, Decimal("3.00"), Decimal("1.50")],
        "ModifiedDate": [D(2008, 3, 11), D(2008, 6, 1), D(2008, 6, 1), D(2007, 1, 1), D(2008, 2, 2), D(2008, 5, 5)],
        "Thumbnail": [b"x"] * 6,
    },
    ("saleslt", "SalesLT_Customer"): {"CustomerID": [1, 2, 3], "CompanyName": ["A", "B", "C"]},
    ("saleslt", "Extra"): {"Id": [1, 2]},
    ("nexora_test", "dim_product_dupes"): {
        "ProductID": [1, 1, 2, 2, 3, 3, 4, 5, None],
        "Name": list("abcdefghi"),
        "Category": ["x"] * 9,
    },
    ("nexora_test", "dim_empty"): {"EmptyID": [], "Label": []},
    ("nexora_test", "dim_nulls"): {"NullKey": [None, None], "Note": ["a", "b"]},
    ("nexora_test", "dim_big"): {"BigID": [1, 2, 3]},
}
FAKE_ROW_COUNT = {("nexora_test", "dim_big"): 20_000_000}
ERRORS = {
    ("nexora_test", "dim_gone"): ("42S02", "[42S02] Invalid object name 'nexora_test.dim_gone'"),
    ("nexora_test", "dim_slow"): ("HYT00", "[HYT00] Query timeout expired"),
}
TOKEN = re.compile(
    r"SUM\(CASE WHEN \[(?P<n>[^\]]+)\] IS NULL"
    r"|APPROX_COUNT_DISTINCT\(\[(?P<a>[^\]]+)\]\)"
    r"|COUNT\(DISTINCT \[(?P<d>[^\]]+)\]\)"
    r"|MIN\(\[(?P<lo>[^\]]+)\]\)"
    r"|MAX\(\[(?P<hi>[^\]]+)\]\)"
)


class FakeCursor:
    def __init__(self, log):
        self.log = log
        self.row = None

    def execute(self, sql):
        self.log.append(sql)
        assert sql.lstrip().upper().startswith("SELECT"), "only SELECT statements are allowed"
        target = re.search(r"FROM \[([^\]]+)\]\.\[([^\]]+)\]", sql)
        key = (target.group(1), target.group(2))
        if key in ERRORS:
            raise Exception(*ERRORS[key])
        rows = DATA[key]
        count = len(next(iter(rows.values())))
        if sql.startswith("SELECT COUNT_BIG(*) FROM ["):
            self.row = (FAKE_ROW_COUNT.get(key, count),)
        elif "HAVING COUNT(*) > 1" in sql:
            column = re.search(r"GROUP BY \[([^\]]+)\]", sql).group(1)
            values = [v for v in rows[column] if v is not None]
            self.row = (sum(1 for v in set(values) if values.count(v) > 1),)
        else:
            out = []
            for m in TOKEN.finditer(sql):
                if m["n"]:
                    out.append(sum(1 for v in rows[m["n"]] if v is None))
                elif m["a"] or m["d"]:
                    out.append(len({v for v in rows[m["a"] or m["d"]] if v is not None}))
                elif m["lo"]:
                    vals = [v for v in rows[m["lo"]] if v is not None]
                    out.append(min(vals) if vals else None)
                else:
                    vals = [v for v in rows[m["hi"]] if v is not None]
                    out.append(max(vals) if vals else None)
            self.row = tuple(out)

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, log):
        self.log = log
        self.timeout = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.log)


class FakeSql:
    def __init__(self):
        self.log: list[str] = []
        self.connection = None

    def connect(self):
        self.connection = FakeConnection(self.log)
        return self.connection


def main() -> None:
    # --- helpers ---
    check("quote_identifier escapes ]", quote_identifier("a]b") == "[a]]b]")
    check("column kinds", [column_kind(t) for t in ("INT", "decimal(8,2)", "TIMESTAMP", "STRING", "BINARY", "ARRAY<INT>", None)]
          == ["numeric", "numeric", "date", "other", "skip", "skip", "skip"])
    check("timeout classified", classify_error(Exception("HYT00", "Query timeout expired"))[0] == "QUERY_TIMEOUT")
    check("missing table classified", classify_error(Exception("42S02", "Invalid object name"))[0] == "TABLE_NOT_FOUND")
    check("other error hides detail", "secret" not in classify_error(Exception("28000", "secret detail"))[1])

    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        sql = FakeSql()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn="tester@contoso.com", name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(entities=("Products",), harvest=("saleslt", "nexora_test"), endpoint=True, state=RunState.REQUIREMENT_APPROVED):
            run = store.create("Show sales by product")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=list(harvest),
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse") if endpoint else None,
            )
            run.state = state
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Sales", entities=list(entities)), run_dir / "BusinessContract.yaml")
            if harvest and state is RunState.REQUIREMENT_APPROVED:
                assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            return run.run_id

        def inventory(rid):
            return json.loads((runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))

        def profile_of(rid, key):
            schema, table = key.split(".")
            return next(t for t in inventory(rid)["tables"] if (t["schema"], t["table_name"]) == (schema, table))["profile"]

        # Candidates from the contract entity "Products" + a requested table
        rid = new_run()
        r = client.post(f"/runs/{rid}/profile", json={"tables": ["nexora_test.dim_empty"]})
        check("profile is 200", r.status_code == 200)
        profiled = [f"{t['schema']}.{t['table_name']}" for t in inventory(rid)["tables"] if t["profile"]]
        check(
            "entity matches plus requested tables are profiled",
            profiled == ["nexora_test.dim_empty", "nexora_test.dim_product_dupes", "saleslt.SalesLT_Product"],
        )
        check("other tables untouched", all(t["profile"] is None for t in inventory(rid)["tables"] if t["table_name"] in ("SalesLT_Customer", "Extra", "dim_nulls")))

        # AC1 row count, AC2 unique key
        product = profile_of(rid, "saleslt.SalesLT_Product")
        check("row count matches the table", product["row_count"] == 6)
        pid = product["columns"]["ProductID"]
        check("unique key: is_unique true and distinct equals row count", pid["is_unique"] is True and pid["distinct_count"] == 6 and pid["null_count"] == 0 and pid["is_key_candidate"] is True)
        check("unique table has status ok", product["status"] in ("ok", "warning"))
        check("Name is not a key candidate, is_unique null", product["columns"]["Name"]["is_key_candidate"] is False and product["columns"]["Name"]["is_unique"] is None)

        # AC3 duplicates
        dupes = profile_of(rid, "nexora_test.dim_product_dupes")
        check(
            "duplicates detected with count matching the GROUP BY HAVING query",
            dupes["columns"]["ProductID"]["is_unique"] is False
            and dupes["columns"]["ProductID"]["duplicate_count"] == 3
            and dupes["status"] == "warning"
            and "ProductID is not unique (3 duplicate values)" in dupes["warnings"],
        )
        check("key with a null gets a warning", any("ProductID has 1 null" in w for w in dupes["warnings"]))

        # AC4 date coverage, numeric min/max, no strings
        check("date min and max", product["columns"]["ModifiedDate"]["min"] == "2007-01-01T00:00:00Z" and product["columns"]["ModifiedDate"]["max"] == "2008-06-01T00:00:00Z")
        check("numeric min and max, decimals as numbers", product["columns"]["Weight"]["min"] == 1.5 and product["columns"]["Weight"]["max"] == 3 and product["columns"]["Weight"]["null_count"] == 2)
        check("no min or max for strings", product["columns"]["Name"]["min"] is None and product["columns"]["Name"]["max"] is None)
        check("freshness uses the modified date column", product["freshness"]["column"] == "ModifiedDate" and product["freshness"]["max_value"] == "2008-06-01T00:00:00Z" and product["freshness"]["status"] == "ok" and product["freshness"]["age_days"] > 0)
        check("binary column skipped with a note", product["columns"]["Thumbnail"]["null_count"] is None and "not profiled" in product["columns"]["Thumbnail"]["note"])

        # AC5 no raw strings
        check("no raw string values in the inventory file", SECRET_NAME not in (runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))
        check("evidence lists the SQL and a timestamp", product["evidence"]["profiled_at"] and product["evidence"]["queries"][0] == "SELECT COUNT_BIG(*) FROM [saleslt].[SalesLT_Product]")

        # Edge cases
        empty = profile_of(rid, "nexora_test.dim_empty")
        check("empty table: row_count 0, warning, uniqueness null", empty["row_count"] == 0 and empty["status"] == "warning" and "The table is empty" in empty["warnings"] and empty["columns"]["EmptyID"]["is_unique"] is None)
        check("empty table: freshness unknown", empty["freshness"]["status"] == "unknown")

        r = client.post(f"/runs/{rid}/profile", json={"tables": ["nexora_test.dim_nulls", "nexora_test.dim_gone", "nexora_test.dim_slow", "nexora_test.dim_big"]})
        body = r.json()
        nulls = profile_of(rid, "nexora_test.dim_nulls")
        check("all-null column warns and null_count equals row_count", nulls["columns"]["NullKey"]["null_count"] == 2 and "NullKey is entirely null" in nulls["warnings"])
        gone = profile_of(rid, "nexora_test.dim_gone")
        check("removed table: error TABLE_NOT_FOUND", gone["status"] == "error" and gone["code"] == "TABLE_NOT_FOUND")
        slow = profile_of(rid, "nexora_test.dim_slow")
        check("timeout: error on that table only", slow["status"] == "error" and slow["code"] == "QUERY_TIMEOUT")
        big = profile_of(rid, "nexora_test.dim_big")
        check(
            "over 10M rows uses APPROX_COUNT_DISTINCT and approximate true",
            big["row_count"] == 20_000_000
            and big["columns"]["BigID"]["approximate"] is True
            and big["columns"]["BigID"]["is_unique"] is None
            and any("APPROX_COUNT_DISTINCT" in q for q in big["evidence"]["queries"]),
        )
        check("other tables still profiled after errors", profile_of(rid, "saleslt.SalesLT_Product")["row_count"] == 6)
        check("summary counts", body["profiled"] == 4 and body["warnings"] == 2 and body["errors"] == 2 and set(body) == {"profiled", "warnings", "errors"})
        check("only SELECT statements ran and timeout was set", all(q.lstrip().upper().startswith("SELECT") for q in sql.log) and sql.connection.timeout == 120)

        # Inventory file, artifact and state
        run = store.get(rid)
        data = (runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8")
        from app.workflow.harvest_stage import canonical_hash

        check("artifact hash tracks the file, version unchanged", run.artifacts["MetadataInventory"].hash == canonical_hash(json.loads(data)) and run.artifacts["MetadataInventory"].version == 1)
        check("versioned copy matches", (runs_root / rid / "inventories" / "MetadataInventory.v1.json").read_text(encoding="utf-8") == data)
        check("state unchanged by profiling", run.state is RunState.REQUIREMENT_APPROVED)
        check("GET inventory returns profiles", client.get(f"/runs/{rid}/inventory").json()["tables"][0]["profile"] is not None)

        # Re-harvest invalidates profiles
        client.post(f"/runs/{rid}/harvest")
        check("re-harvest clears profiles", all(t["profile"] is None for t in inventory(rid)["tables"]) and inventory(rid)["version"] == 2)

        # Entity matching
        rid2 = new_run(entities=("Customers",), harvest=("saleslt",))
        r = client.post(f"/runs/{rid2}/profile")
        check("entity Customers selects only the customer table", r.status_code == 200 and r.json()["profiled"] == 1 and profile_of(rid2, "saleslt.SalesLT_Customer")["row_count"] == 3)
        rid3 = new_run(entities=("Warehouses",), harvest=("saleslt",))
        r = client.post(f"/runs/{rid3}/profile")
        check("no candidate and no request profiles nothing", r.status_code == 200 and r.json() == {"profiled": 0, "warnings": 0, "errors": 0})

        # Errors
        check("table not in inventory is 404", envelope(client.post(f"/runs/{rid2}/profile", json={"tables": ["saleslt.Nope"]}), 404, "NOT_FOUND"))
        check("bad table name is 422", envelope(client.post(f"/runs/{rid2}/profile", json={"tables": ["nodot"]}), 422, "VALIDATION_ERROR"))
        check("case-insensitive table names", client.post(f"/runs/{rid2}/profile", json={"tables": ["SALESLT.extra"]}).status_code == 200 and profile_of(rid2, "saleslt.Extra")["row_count"] == 2)
        check("wrong state is 409", envelope(client.post(f"/runs/{new_run(state=RunState.CREATED)}/profile"), 409, "WRONG_STATE"))
        check("no SQL endpoint is 400", envelope(client.post(f"/runs/{new_run(endpoint=False)}/profile"), 400, "BAD_REQUEST"))
        rid4 = store.create("x").run_id
        run4 = store.get(rid4)
        run4.state = RunState.REQUIREMENT_APPROVED
        run4.scope = RunScope(workspace_id=WS, lakehouse_id=LH, lakehouse_name="L", sql_endpoint=SqlEndpoint(server="s", database="d"))
        store.save(run4)
        check("profile before harvest is 404", envelope(client.post(f"/runs/{rid4}/profile"), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/profile"), 404, "RUN_NOT_FOUND"))

        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
