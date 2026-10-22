"""T-403 offline checks: bridge detection, mapping-table validation, derivation rules, approvals
and the POST /runs/{id}/plan/bridges endpoint.

Run: python -m app.manual_tests.t403_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse, SQL endpoint and model are fakes, and roles, conformed sources, the glossary and
the derivation rules are temporary files (extending the fixtures of t302, t303 and t401), so no
sign-in or network is needed. The live steps are in MANUAL_TESTING.md.
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
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.discovery import derivation_rules as rules_module
from app.discovery import glossary as glossary_module
from app.discovery.derivation_rules import load_rules
from app.compiler.critical_terms import ConfigError
from app.manual_tests import t302_offline_checks as base
from app.manual_tests import t303_offline_checks as t303  # extends the t302 fixtures
from app.manual_tests import t401_offline_checks as t401  # extends them further
from app.store.file_store import FileRunStore
from app.workflow import conformed as conformed_module
from app.workflow import roles as roles_module
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

check = base.check
col = base.col
WS, LH, RANDOM = base.WS, base.LH, base.RANDOM
UPN = "tester@contoso.com"
P, C, D, H = t401.P, t401.C, t401.D, t401.H
PROMO, BRIDGE, CAMPAIGN, DUPES = "nexora_test.dim_promotion", "nexora_test.bridge_product_promotion", "nexora_test.dim_campaign", "nexora_test.dim_product_dupes"
ALT = "nexora_test.bridge_alt"

# --- fixtures on top of t401: the bridge scenarios of fixtures/README.md --------------------
base.LAKEHOUSE["nexora_test"].update(
    {
        "dim_promotion": [col("PromotionID", "INT"), col("PromotionName", "STRING")],
        "bridge_product_promotion": [col("ProductID", "INT"), col("PromotionID", "INT")],
        "dim_campaign": [col("CampaignID", "INT"), col("CampaignName", "STRING"), col("ProductCategory", "STRING")],
    }
)
base.DATA[("nexora_test", "dim_promotion")] = {"PromotionID": [1, 2, 3, 4], "PromotionName": list("abcd")}
BRIDGE_PAIRS = [(680, 1), (706, 1), (707, 2), (680, 2), (706, 3), (707, 4)]
base.DATA[("nexora_test", "bridge_product_promotion")] = {
    "ProductID": [p for p, _ in BRIDGE_PAIRS],
    "PromotionID": [q for _, q in BRIDGE_PAIRS],
}
base.DATA[("nexora_test", "dim_campaign")] = {"CampaignID": [1, 2], "CampaignName": ["x", "y"], "ProductCategory": ["Bikes", "Parts"]}

PAIR_COLUMNS = re.compile(r"SELECT \[(\w+)\], \[(\w+)\] FROM \[(\w+)\]\.\[(\w+)\] WHERE")
DISTINCT_K = re.compile(r"SELECT DISTINCT \[(\w+)\] AS \[k\] FROM \[(\w+)\]\.\[(\w+)\] WHERE")


class BridgeCursor(t303.EvidenceCursor):
    """Adds the two aggregate queries the bridge validation runs."""

    def execute(self, sql):
        if "COUNT_BIG(*) > 1) AS d" in sql:
            self.log.append(sql)
            assert sql.lstrip().upper().startswith("SELECT"), "only SELECT statements are allowed"
            a, b, schema, table = PAIR_COLUMNS.search(sql).groups()
            rows = list(zip(base.DATA[(schema, table)][a], base.DATA[(schema, table)][b]))
            rows = [r for r in rows if None not in r]
            self.row = (sum(1 for r in set(rows) if rows.count(r) > 1),)
        elif "m.[k] IS NOT NULL" in sql:
            self.log.append(sql)
            assert sql.lstrip().upper().startswith("SELECT"), "only SELECT statements are allowed"
            (t_col, t_schema, t_table), (m_col, m_schema, m_table) = DISTINCT_K.findall(sql)
            target = {v for v in base.DATA[(t_schema, t_table)][t_col] if v is not None}
            mapped = {v for v in base.DATA[(m_schema, m_table)][m_col] if v is not None}
            self.row = (len(target), len(target & mapped))
        else:
            super().execute(sql)


class BridgeConnection(base.FakeConnection):
    def cursor(self):
        return BridgeCursor(self.log)


class BridgeSql(base.FakeSql):
    def connect(self):
        self.connection = BridgeConnection(self.log)
        return self.connection


def main() -> None:
    # --- derivation_rules.yaml ---
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "derivation_rules.yaml"
        try:
            load_rules(path)
            named = False
        except ConfigError as exc:
            named = "derivation_rules.yaml" in str(exc)
        check("missing derivation_rules.yaml raises ConfigError naming the file", named)
        cases = {
            "not a mapping": "- a\n",
            "not a list": "rules: x\n",
            "one entity": "rules:\n  - {id: R, entities: [A], description: d, approved_by: u}\n",
            "no approver": "rules:\n  - {id: R, entities: [A, B], description: d}\n",
            "bad flag": "rules:\n  - {id: R, entities: [A, B], description: d, approved_by: u, deduplicate: maybe}\n",
            "duplicate ids": "rules:\n  - {id: R, entities: [A, B], description: d, approved_by: u}\n  - {id: R, entities: [A, C], description: d, approved_by: u}\n",
        }
        for label, body in cases.items():
            path.write_text(body, encoding="utf-8")
            try:
                load_rules(path)
                message = ""
            except ConfigError as exc:
                message = str(exc)
            check(f"invalid derivation_rules.yaml ({label}) names the file", "derivation_rules.yaml" in message)
        path.write_text("rules:\n  - {id: DR1, entities: [Product, Region], description: d, approved_by: u}\n", encoding="utf-8")
        rule = load_rules(path)[0]
        check("a rule covers its entity pair in either order, ignoring case", rule.covers("region", "PRODUCT") and not rule.covers("Product", "Customer"))
        path.write_text("rules:\n", encoding="utf-8")
        check("an empty rules list is valid", load_rules(path) == [])
        check("the shipped file loads", isinstance(load_rules(), list))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = BridgeSql()
        roles_file, conformed_file, glossary_file, rules_file = (tmp_dir / n for n in ("roles.yaml", "conformed.yaml", "glossary.yaml", "rules.yaml"))
        roles_file.write_text(f"roles:\n  bi_engineer: [{UPN}]\n", encoding="utf-8")
        t401.write_conformed(conformed_file, [P, C, D, H, PROMO, BRIDGE, CAMPAIGN, DUPES])
        glossary_file.write_text(
            "terms:\n  product: [product, products]\n  customer: [customer, customers]\n  sales: [sales, salesorderdetail]\n",
            encoding="utf-8",
        )
        rules_file.write_text("rules: []\n", encoding="utf-8")
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = (
            roles_file, conformed_file, glossary_file, rules_file,
        )

        source_llm = t401.FakeSourceLlm({"entities": [], "kpi_fields": []})
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        user = {"upn": UPN}
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=user["upn"], name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: base.FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_source_llm] = lambda: source_llm
        app.dependency_overrides[get_relationship_llm] = lambda: (lambda s, u, sc: json.dumps({"relationships": []}))
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(entities, analyze=True, resolve=True, endpoint=True):
            run = store.create("Show sales by product and promotion")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=["saleslt", "nexora_test"],
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse") if endpoint else None,
            )
            run.state = RunState.REQUIREMENT_APPROVED
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Sales", entities=list(entities)), run_dir / "BusinessContract.yaml")
            assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            if endpoint:
                assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
                if analyze:
                    assert client.post(f"/runs/{run.run_id}/relationships/analyze").status_code == 200
            run = store.get(run.run_id)
            run.state = RunState.DATA_PROFILED
            store.save(run)
            if resolve:
                assert client.post(f"/runs/{run.run_id}/plan/sources").status_code == 200
            return run.run_id

        def bridges(rid):
            return client.post(f"/runs/{rid}/plan/bridges")

        def artifact_hash(rid):
            return store.get(rid).artifacts["BridgeProposals"].hash

        def decide(rid, bridge_id, action="ACCEPT", expected=None, **extra):
            return client.post(
                f"/runs/{rid}/approvals",
                json={"artifact": f"BridgeProposal:{bridge_id}", "action": action, "expected_hash": expected or artifact_hash(rid), **extra},
            )

        # AC1: existing mapping proposed
        rid = new_run(["Sales", "Product", "Promotion"])
        sql.log.clear()
        r = bridges(rid)
        body = r.json()
        check("an existing mapping table is proposed: 200 and one proposal", r.status_code == 200 and len(body) == 1)
        b1 = body[0]
        check("B1 is an existing_table for Product and Promotion", b1["id"] == "B1" and b1["entities"] == ["Product", "Promotion"] and b1["situation"] == "existing_table"
              and b1["table"] == BRIDGE and b1["derivation_rule"] is None and b1["status"] == "proposed" and b1["issues"] == [])
        check("evidence: no duplicate pairs and the key coverage on each side",
              b1["evidence"]["duplicate_pairs"] == 0 and b1["evidence"]["coverage_a_pct"] == 50.0 and b1["evidence"]["coverage_b_pct"] == 100.0)
        check("the evidence queries are read-only aggregates", b1["evidence"]["queries"] and all(q.lstrip().upper().startswith("SELECT") for q in b1["evidence"]["queries"])
              and all("COUNT_BIG" in q for q in b1["evidence"]["queries"]))
        check("a found bridge leaves the run in DATA_PROFILED", store.get(rid).state is RunState.DATA_PROFILED)
        ref = store.get(rid).artifacts["BridgeProposals"]
        check("BridgeProposals.json is saved and registered as a draft", (runs_root / rid / "BridgeProposals.json").is_file() and ref.version == 1 and ref.status == "draft"
              and "SourceResolution" in ref.based_on and "MetadataInventory" in ref.based_on)

        first = (runs_root / rid / "BridgeProposals.json").read_bytes()
        r = bridges(rid)
        check("a second call gives an identical file and version", r.status_code == 200 and (runs_root / rid / "BridgeProposals.json").read_bytes() == first
              and store.get(rid).artifacts["BridgeProposals"].version == 1)

        # approval
        r = decide(rid, "B1", "ACCEPT", expected="sha256:old")
        check("a stale hash is 409 STALE_VERSION", envelope(r, 409, "STALE_VERSION"))
        user["upn"] = "someone.else@contoso.com"
        check("a user without bi_engineer or business_owner is 403", envelope(decide(rid, "B1"), 403, "FORBIDDEN"))
        user["upn"] = UPN
        check("EXPLAIN is not available for a bridge (422)", envelope(decide(rid, "B1", "EXPLAIN"), 422, "VALIDATION_ERROR"))
        check("REJECT needs a comment (422)", envelope(decide(rid, "B1", "REJECT"), 422, "VALIDATION_ERROR"))
        check("an unknown proposal is 404", envelope(decide(rid, "B9"), 404, "NOT_FOUND"))
        check("nothing was recorded by the refused actions", not (runs_root / rid / "approvals.jsonl").exists())
        r = decide(rid, "B1", comment="Mapping is maintained by the pricing team")
        record = json.loads((runs_root / rid / "approvals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        check("a bi_engineer approves: 200, the approval record names the proposal and role", r.status_code == 200 and record["artifact"] == "BridgeProposal:B1" and record["action"] == "ACCEPT"
              and record["role"] == "bi_engineer" and record["user"]["upn"] == UPN and record["artifact_hash"] == artifact_hash(rid) and record["comment"].startswith("Mapping"))
        check("the proposal is approved and the artifact too, as a new version", json.loads((runs_root / rid / "BridgeProposals.json").read_text(encoding="utf-8"))["proposals"][0]["status"] == "approved"
              and store.get(rid).artifacts["BridgeProposals"].status == "approved" and store.get(rid).artifacts["BridgeProposals"].version == 2)
        check("approving again is 409 ALREADY_APPROVED", envelope(decide(rid, "B1"), 409, "ALREADY_APPROVED"))
        r = bridges(rid)
        check("a re-run keeps the approval and the version when nothing changed", r.json()[0]["status"] == "approved" and store.get(rid).artifacts["BridgeProposals"].version == 2)
        roles_file.write_text(f"roles:\n  business_owner: [{UPN}]\n", encoding="utf-8")
        rid_b = new_run(["Product", "Promotion"])
        bridges(rid_b)
        r = decide(rid_b, "B1", "REJECT", comment="Wrong table")
        record = json.loads((runs_root / rid_b / "approvals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        check("a business_owner can also decide; a rejection is recorded with the comment", r.status_code == 200 and record["role"] == "business_owner" and record["action"] == "REJECT"
              and json.loads((runs_root / rid_b / "BridgeProposals.json").read_text(encoding="utf-8"))["proposals"][0]["status"] == "rejected")
        roles_file.write_text(f"roles:\n  bi_engineer: [{UPN}]\n", encoding="utf-8")

        # AC2 and AC3: no guess
        rid2 = new_run(["Product", "Campaign"])
        r = bridges(rid2)
        p = r.json()[0]
        check("no mapping table and no rule: needs_rules, no table proposed", r.status_code == 200 and len(r.json()) == 1 and p["situation"] == "needs_rules"
              and p["table"] is None and p["derivation_rule"] is None and p["entities"] == ["Product", "Campaign"] and "mapping rules required" in p["issues"][0])
        run = store.get(rid2)
        check("the run is NEEDS_INPUT with a data gap asking for the mapping rules", run.state is RunState.NEEDS_INPUT and run.resume_state is RunState.DATA_PROFILED
              and "Mapping rules are required for Product and Campaign" in json.loads(client.get(f"/runs/{rid2}").text)["data_gaps"][0]["description"])
        check("the artifact is marked needs_input", run.artifacts["BridgeProposals"].status == "needs_input")
        r = decide(rid2, "B1")
        check("approving a needs_rules proposal is 409 MAPPING_RULES_REQUIRED", envelope(r, 409, "MAPPING_RULES_REQUIRED") and "mapping rules required" in r.json()["error"]["message"])
        check("it stays proposed and nothing is recorded", json.loads((runs_root / rid2 / "BridgeProposals.json").read_text(encoding="utf-8"))["proposals"][0]["status"] == "proposed"
              and not (runs_root / rid2 / "approvals.jsonl").exists())
        check("a run in NEEDS_INPUT can't be analyzed again (409)", envelope(bridges(rid2), 409, "WRONG_STATE"))

        # two entities in the same contract: one proposal lists the others
        rid3 = new_run(["Sales", "Product", "Campaign"])
        r = bridges(rid3)
        check("an isolated entity gets one needs_rules proposal naming the entities it can't reach", len(r.json()) == 1 and r.json()[0]["entities"] == ["Sales", "Product", "Campaign"])

        # no false positives on a normal star, and an ignored many-to-many
        rid4 = new_run(["Sales", "Product", "Customer"])
        r = bridges(rid4)
        check("a normal star (Sales, Product, Customer) needs no bridge: no proposals, state unchanged", r.status_code == 200 and r.json() == [] and store.get(rid4).state is RunState.DATA_PROFILED)
        rid4b = new_run(["Sales", "Product"])
        evidence = json.loads((runs_root / rid4b / "MetadataInventory.json").read_text(encoding="utf-8"))["relationship_evidence"]
        check("a many-to-many nobody needs exists (into dim_product_dupes) and is ignored", any(e["cardinality"] == "many_to_many" for e in evidence) and bridges(rid4b).json() == [])

        # a many-to-many between two entities the contract needs
        rid5 = new_run(["Product", "Dupes"])
        r = bridges(rid5)
        check("a direct many-to-many between two needed entities needs a bridge, and without rules needs_rules",
              len(r.json()) == 1 and r.json()[0]["entities"] == ["Product", "Dupes"] and r.json()[0]["situation"] == "needs_rules")

        # derivation rule
        rules_file.write_text("rules:\n  - {id: DR1, entities: [Campaign, Product], description: Campaign applies to products of its category, approved_by: u@contoso.com}\n", encoding="utf-8")
        rid6 = new_run(["Product", "Campaign"])
        r = bridges(rid6)
        p = r.json()[0]
        check("an approved derivation rule gives a derivation_rule proposal, with no NEEDS_INPUT", p["situation"] == "derivation_rule" and p["derivation_rule"] == "DR1" and p["table"] is None
              and store.get(rid6).state is RunState.DATA_PROFILED)
        check("a derivation_rule proposal can be approved", decide(rid6, "B1").status_code == 200)

        # duplicate pairs
        rules_file.write_text("rules: []\n", encoding="utf-8")
        base.DATA[("nexora_test", "bridge_product_promotion")]["ProductID"].append(680)
        base.DATA[("nexora_test", "bridge_product_promotion")]["PromotionID"].append(1)
        rid7 = new_run(["Product", "Promotion"])
        p = bridges(rid7).json()[0]
        check("duplicate pairs are reported in the issues", p["evidence"]["duplicate_pairs"] == 1 and "duplicate pairs: 1" in p["issues"] and p["deduplication_rule"] is None)
        r = decide(rid7, "B1")
        check("duplicates without an approved deduplication rule can't be approved (409)", envelope(r, 409, "MAPPING_RULES_REQUIRED") and "duplicate pairs" in r.json()["error"]["message"])
        rules_file.write_text("rules:\n  - {id: DR2, entities: [Product, Promotion], description: Keep one row per pair, approved_by: u@contoso.com, deduplicate: true}\n", encoding="utf-8")
        rid8 = new_run(["Product", "Promotion"])
        p = bridges(rid8).json()[0]
        check("with an approved deduplication rule the proposal names it and can be approved", p["deduplication_rule"] == "DR2" and decide(rid8, "B1").status_code == 200)
        rules_file.write_text("rules: []\n", encoding="utf-8")
        base.DATA[("nexora_test", "bridge_product_promotion")]["ProductID"].pop()
        base.DATA[("nexora_test", "bridge_product_promotion")]["PromotionID"].pop()

        # several mapping tables
        base.LAKEHOUSE["nexora_test"]["bridge_alt"] = [col("ProductID", "INT"), col("PromotionID", "INT")]
        base.DATA[("nexora_test", "bridge_alt")] = {"ProductID": [680, 706, 707, 708], "PromotionID": [1, 2, 3, 4]}
        t401.write_conformed(conformed_file, [P, C, D, H, PROMO, BRIDGE, CAMPAIGN, DUPES, ALT])
        rid9 = new_run(["Product", "Promotion"])
        p = bridges(rid9).json()[0]
        check("with several mapping tables the highest coverage is proposed and the others are listed", p["table"] == ALT and p["evidence"]["coverage_a_pct"] == 66.7
              and any(i.startswith(f"other mapping table candidate: {BRIDGE}") for i in p["issues"]))

        # errors
        check("before sources are resolved is 404 NOT_FOUND", envelope(bridges(new_run(["Product", "Promotion"], resolve=False)), 404, "NOT_FOUND"))
        r = bridges(new_run(["Product", "Promotion"], analyze=False))
        check("before relationships are analyzed is 400 BAD_REQUEST", envelope(r, 400, "BAD_REQUEST") and "analyze" in r.json()["error"]["message"])
        r = bridges(new_run(["Product", "Promotion"], endpoint=False, resolve=False))
        check("a Lakehouse with no SQL endpoint is 400 BAD_REQUEST", envelope(r, 400, "BAD_REQUEST") and "SQL endpoint" in r.json()["error"]["message"])
        run_x = new_run(["Product", "Promotion"])
        run = store.get(run_x)
        run.state = RunState.REQUIREMENT_APPROVED
        store.save(run)
        check("wrong state is 409 WRONG_STATE", envelope(bridges(run_x), 409, "WRONG_STATE"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/plan/bridges"), 404, "RUN_NOT_FOUND"))
        rules_file.write_text("rules: x\n", encoding="utf-8")
        run_y = new_run(["Product", "Promotion"])
        check("an invalid derivation_rules.yaml is 500 CONFIG_ERROR", envelope(bridges(run_y), 500, "CONFIG_ERROR"))
        check("every bridge query was a SELECT", all(q.lstrip().upper().startswith("SELECT") for q in sql.log))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
