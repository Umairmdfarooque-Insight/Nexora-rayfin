"""T-303 offline checks: relationship candidates, measured evidence and the relationship endpoints.

Run: python -m app.manual_tests.t303_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The SQL endpoint and the LLM are fakes (the SQL fake evaluates the evidence queries against
in-memory tables, extending the one in t302_offline_checks), so no sign-in or network is needed.
The live steps are in MANUAL_TESTING.md.
"""

import json
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_relationship_llm,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler.critical_terms import ConfigError
from app.contracts import business_contract as bc
from app.discovery.relationship_evidence import load_thresholds, type_family, types_compatible
from app.llm.errors import LlmTimeout
from app.manual_tests import t302_offline_checks as base
from app.store.file_store import FileRunStore
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

WS, LH, RANDOM = base.WS, base.LH, base.RANDOM
col = base.col

# --- extra tables on top of the T-302 fixtures ------------------------------------------
base.LAKEHOUSE["saleslt"].update(
    {
        "SalesLT_SalesOrderDetail": [
            col("SalesOrderDetailID", "INT", False), col("ProductID", "INT"), col("OrderQty", "INT"),
        ],
        "SalesLT_SalesOrderHeader": [col("SalesOrderID", "INT", False), col("CustomerID", "INT")],
        "SalesLT_CustomerProfile": [col("CustomerID", "INT", False), col("Segment", "STRING")],
    }
)
DETAIL_PRODUCTS = [680, 706, 707, 708, 709, 710, 680, 706, 707, 710]
HEADER_CUSTOMERS = [1] * 8 + [2] * 6 + [3] * 4 + [9] + [None]
base.DATA[("saleslt", "SalesLT_SalesOrderDetail")] = {
    "SalesOrderDetailID": list(range(1, 11)),
    "ProductID": DETAIL_PRODUCTS,
    "OrderQty": [1] * 10,
}
base.DATA[("saleslt", "SalesLT_SalesOrderHeader")] = {
    "SalesOrderID": list(range(1, 21)),
    "CustomerID": HEADER_CUSTOMERS,
}
base.DATA[("saleslt", "SalesLT_CustomerProfile")] = {"CustomerID": [1, 2, 3], "Segment": ["a", "b", "c"]}

DISTINCT = re.compile(r"\(SELECT DISTINCT \[([^\]]+)\] AS \[k\] FROM \[([^\]]+)\]\.\[([^\]]+)\] WHERE")


class EvidenceCursor(base.FakeCursor):
    def execute(self, sql):
        if "AS [k]" not in sql:
            return super().execute(sql)
        self.log.append(sql)
        assert sql.lstrip().upper().startswith("SELECT"), "only SELECT statements are allowed"
        parts = DISTINCT.findall(sql)
        t_col, t_schema, t_table = parts[-1]
        target = {v for v in base.DATA[(t_schema, t_table)][t_col] if v is not None}
        if "FROM (SELECT DISTINCT" in sql:  # overlap: distinct source values
            s_col, s_schema, s_table = parts[0]
            values = {v for v in base.DATA[(s_schema, s_table)][s_col] if v is not None}
            self.row = (len(values), sum(1 for v in values if v in target))
        else:  # orphans: source rows
            s_schema, s_table = re.search(r"FROM \[([^\]]+)\]\.\[([^\]]+)\] AS s LEFT JOIN", sql).groups()
            s_col = re.search(r"ON s\.\[([^\]]+)\] = t", sql).group(1)
            values = [v for v in base.DATA[(s_schema, s_table)][s_col] if v is not None]
            self.row = (len(values), sum(1 for v in values if v not in target))


class EvidenceConnection(base.FakeConnection):
    def cursor(self):
        return EvidenceCursor(self.log)


class EvidenceSql(base.FakeSql):
    def connect(self):
        self.connection = EvidenceConnection(self.log)
        return self.connection


def main() -> None:
    # --- pure helpers ---
    check = base.check
    check("type families", [type_family(t) for t in ("INT", "LONG", "DECIMAL(8,2)", "STRING", "DATE", "BOOLEAN", None)]
          == ["integer", "integer", "decimal", "string", "date", "boolean", "other:"])
    check("integer and decimal are compatible", types_compatible("INT", "DECIMAL(9,2)") and types_compatible("DATE", "TIMESTAMP"))
    check("int and string are not compatible", not types_compatible("INT", "STRING") and not types_compatible(None, None))
    th = load_thresholds()
    check("thresholds load from config", (th.high_orphan_max, th.high_overlap_min, th.medium_orphan_max, th.require_unique_target) == (1.0, 99.0, 10.0, True))
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "relationship_thresholds.yaml"
        bad.write_text("high: {orphan_rate_max: 'x'}\n", encoding="utf-8")
        try:
            load_thresholds(bad)
            raised = False
        except ConfigError as exc:
            raised = "relationship_thresholds.yaml" in str(exc)
        check("invalid thresholds file is a ConfigError naming the file", raised)

    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        sql = EvidenceSql()
        llm_state = {"fn": None, "payloads": []}

        def llm(system, user, schema):
            llm_state["payloads"].append(user)
            return llm_state["fn"](system, user, schema)

        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn="tester@contoso.com", name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: base.FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_relationship_llm] = lambda: llm
        client = TestClient(app, raise_server_exceptions=False)

        def suggestions(*items):
            return lambda s, u, sc: json.dumps(
                {"relationships": [dict(zip(("from_table", "from_column", "to_table", "to_column"), i), reason="r") for i in items]}
            )

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(state=RunState.REQUIREMENT_APPROVED, endpoint=True, harvest=True):
            run = store.create("Show sales by product")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=["saleslt", "nexora_test"],
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse") if endpoint else None,
            )
            run.state = state
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Sales", entities=["Products"]), run_dir / "BusinessContract.yaml")
            if harvest and state is RunState.REQUIREMENT_APPROVED:
                assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            return run.run_id

        def rel(rid):
            return {(e["from"]["table"], e["from"]["column"], e["to"]["table"], e["to"]["column"]): e for e in client.get(f"/runs/{rid}/relationships").json()}

        P, D, C = "saleslt.SalesLT_Product", "saleslt.SalesLT_SalesOrderDetail", "saleslt.SalesLT_Customer"
        DUPES, HEADER, CP = "nexora_test.dim_product_dupes", "saleslt.SalesLT_SalesOrderHeader", "saleslt.SalesLT_CustomerProfile"

        rid = new_run()
        check("GET relationships before analysis is an empty list", client.get(f"/runs/{rid}/relationships").json() == [])

        llm_state["fn"] = suggestions(
            (D, "ProductID", P, "ProductID"),  # same as a heuristic: kept once
            (D, "ProductID", P, "NoSuchColumn"),  # hallucinated column
            ("saleslt.Ghost", "GhostID", P, "ProductID"),  # hallucinated table
            (P, "ProductID", P, "ProductID"),  # self relationship
            (P, "Name", C, "CustomerID"),  # type mismatch
            (C, "CustomerID", CP, "CustomerID"),  # one to one
            ("nexora_test.dim_gone", "GoneID", P, "ProductID"),  # table can't be profiled
        )
        r = client.post(
            f"/runs/{rid}/relationships/analyze",
            json={"candidates": [{"from": {"table": D, "column": "ProductID"}, "to": {"table": C, "column": "CustomerID"}}]},
        )
        body = r.json()
        check("analyze is 200", r.status_code == 200)
        found = rel(rid)
        check("ids are R1, R2, ... in order", sorted((e["id"] for e in found.values()), key=lambda i: int(i[1:])) == [f"R{i}" for i in range(1, len(found) + 1)])
        check("summary adds up", body["candidates"] == body["high"] + body["medium"] + body["low"] and body["candidates"] + body["rejected"] == len(found) and body["warnings"] == [])

        # AC1: real join measured
        main_join = found[(D, "ProductID", P, "ProductID")]
        check("heuristic found Detail.ProductID -> Product.ProductID", main_join["source"] == "heuristic")
        check(
            "many_to_one, high, 0% orphans, 100% overlap",
            main_join["cardinality"] == "many_to_one" and main_join["confidence"] == "high"
            and main_join["orphan_rate"] == 0.0 and main_join["overlap_pct"] == 100.0 and main_join["null_rate"] == 0.0
            and main_join["type_compatible"] is True and main_join["status"] == "candidate" and main_join["rejected_reason"] is None,
        )
        check("evidence carries the SQL and timestamp, thresholds from config", len(main_join["evidence"]["queries"]) == 2 and main_join["evidence"]["computed_at"]
              and main_join["thresholds"]["high"] == {"orphan_rate_max": 1.0, "overlap_pct_min": 99.0})
        check("the same join suggested by the LLM is not duplicated", sum(1 for k in found if k[:4] == (D, "ProductID", P, "ProductID")) == 1)

        # null rate, orphan rate, medium
        header = found[(HEADER, "CustomerID", C, "CustomerID")]
        non_null = [v for v in HEADER_CUSTOMERS if v is not None]
        check(
            "orphan rate matches a hand computation; null rate reported; medium confidence",
            header["orphan_rate"] == round(sum(1 for v in non_null if v not in (1, 2, 3)) * 100 / len(non_null), 2)
            and header["null_rate"] == 5.0 and header["overlap_pct"] == 75.0 and header["confidence"] == "medium"
            and header["cardinality"] == "many_to_one",
        )

        # AC2: bad join is low with the overlap shown
        by_hand = found[(D, "ProductID", C, "CustomerID")]
        check("user-supplied bad join: source user, overlap 0, low", by_hand["source"] == "user" and by_hand["overlap_pct"] == 0.0 and by_hand["confidence"] == "low")

        # AC3: dupes target
        to_dupes = found[(D, "ProductID", DUPES, "ProductID")]
        check(
            "join into the duplicates table is many_to_many, low, with the reason",
            to_dupes["cardinality"] == "many_to_many" and to_dupes["confidence"] == "low"
            and any("not unique" in n for n in to_dupes["notes"]),
        )
        from_dupes = found[(DUPES, "ProductID", P, "ProductID")]
        check("from the duplicates table the keys don't overlap: low", from_dupes["confidence"] == "low" and from_dupes["overlap_pct"] == 0.0)

        # AC4 and the other rejections
        bad_column = found[(D, "ProductID", P, "NoSuchColumn")]
        check("hallucinated column rejected with the reason", bad_column["status"] == "rejected" and bad_column["rejected_reason"] == "column not in inventory" and bad_column["source"] == "llm" and bad_column["confidence"] is None)
        check("hallucinated table rejected", found[("saleslt.Ghost", "GhostID", P, "ProductID")]["rejected_reason"] == "table not in inventory")
        check("self-relationship rejected", found[(P, "ProductID", P, "ProductID")]["status"] == "rejected" and "self" in found[(P, "ProductID", P, "ProductID")]["rejected_reason"])
        check("a table that can't be profiled rejects with 'profile missing'", found[("nexora_test.dim_gone", "GoneID", P, "ProductID")]["rejected_reason"] == "profile missing")

        # type mismatch: no join query
        mismatch = found[(P, "Name", C, "CustomerID")]
        check(
            "type mismatch: incompatible, low, no join query",
            mismatch["type_compatible"] is False and mismatch["confidence"] == "low"
            and mismatch["evidence"]["queries"] == [] and mismatch["overlap_pct"] is None and mismatch["cardinality"] is None,
        )
        one_to_one = found[(C, "CustomerID", CP, "CustomerID")]
        check("both sides unique: one_to_one, high", one_to_one["cardinality"] == "one_to_one" and one_to_one["confidence"] == "high")

        # Profiles were computed for the tables used, and stored with the evidence
        inv = client.get(f"/runs/{rid}/inventory").json()
        profiled = {f"{t['schema']}.{t['table_name']}" for t in inv["tables"] if t["profile"]}
        check("profiles computed first for the tables used", {P, D, C, HEADER, CP, DUPES} <= profiled and "saleslt.Extra" not in profiled)
        check("evidence stored in MetadataInventory.relationship_evidence", len(inv["relationship_evidence"]) == len(found))
        run = store.get(rid)
        from app.workflow.harvest_stage import canonical_hash

        file = json.loads((runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))
        check("artifact hash tracks the file and state is unchanged", run.artifacts["MetadataInventory"].hash == canonical_hash(file) and run.state is RunState.REQUIREMENT_APPROVED)
        check("only SELECT statements ran", all(q.lstrip().upper().startswith("SELECT") for q in sql.log))

        # The LLM saw names and types only
        payload = json.loads(llm_state["payloads"][0])
        check(
            "LLM payload is the contract and table and column names and types only",
            set(payload) == {"contract", "tables"}
            and all(set(t) == {"table", "columns"} and all(set(c) == {"name", "type"} for c in t["columns"]) for t in payload["tables"])
            and base.SECRET_NAME not in llm_state["payloads"][0],
        )

        # Re-analyzing replaces the evidence (the user candidate is gone)
        llm_state["fn"] = suggestions()
        r = client.post(f"/runs/{rid}/relationships/analyze")
        found2 = rel(rid)
        check("re-analysis replaces the evidence", r.status_code == 200 and (D, "ProductID", C, "CustomerID") not in found2 and (C, "CustomerID", CP, "CustomerID") not in found2)

        # LLM failure degrades to heuristics with a warning
        def failing(s, u, sc):
            raise LlmTimeout("slow")

        llm_state["fn"] = failing
        r = client.post(f"/runs/{rid}/relationships/analyze")
        check("LLM failure: heuristics still analyzed, with a warning", r.status_code == 200 and r.json()["candidates"] > 0 and len(r.json()["warnings"]) == 1 and "LlmTimeout" in r.json()["warnings"][0])
        llm_state["fn"] = lambda s, u, sc: "not json"
        r = client.post(f"/runs/{rid}/relationships/analyze")
        check("unusable LLM output also degrades with a warning", r.status_code == 200 and len(r.json()["warnings"]) == 1)

        # Re-harvest invalidates the evidence
        client.post(f"/runs/{rid}/harvest")
        check("re-harvest clears the evidence", client.get(f"/runs/{rid}/relationships").json() == [])

        # Errors
        llm_state["fn"] = suggestions()
        check("wrong state is 409", envelope(client.post(f"/runs/{new_run(state=RunState.CREATED)}/relationships/analyze"), 409, "WRONG_STATE"))
        check("no SQL endpoint is 400", envelope(client.post(f"/runs/{new_run(endpoint=False)}/relationships/analyze"), 400, "BAD_REQUEST"))
        rid_nh = new_run(harvest=False)
        check("analyze before harvest is 404", envelope(client.post(f"/runs/{rid_nh}/relationships/analyze"), 404, "NOT_FOUND"))
        check("GET before harvest is 404", envelope(client.get(f"/runs/{rid_nh}/relationships"), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/relationships/analyze"), 404, "RUN_NOT_FOUND"))
        check("bad body is 422", envelope(client.post(f"/runs/{rid}/relationships/analyze", json={"candidates": [{"from": {"table": "x"}}]}), 422, "VALIDATION_ERROR"))

        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
