"""T-506 offline checks: bridge generation, the notebook, the G4.BRIDGE.* checks and
`POST /runs/{id}/model/bridges/{bridge_id}/generate`.

Run: python -m app.manual_tests.t506_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI, the Lakehouse, the SQL endpoint and the models are fakes (extending the fixtures of
t502 to t505), and the run is written by hand into a temporary folder, so no sign-in or network is
needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_bridge_llm,
    get_connector_factory,
    get_dax_llm,
    get_items_client,
    get_powerbi_client,
    get_recon_llm,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.bridge_proposal import BridgeProposal, BridgeProposals
from app.contracts.metadata_inventory import (
    ColumnRef,
    InventoryColumn,
    InventoryTable,
    RelationshipEvidence,
    RelationshipQueryEvidence,
)
from app.contracts.model_plan import ModelColumn, ModelSpec, ModelTable, TableSource, load_yaml, save_yaml, to_data
from app.contracts.source_resolution import EntityResolution, SelectedSource, SourceResolution
from app.deploy.fabric_items import FabricItemsClient, NotebookRunFailed, TargetNotAllowed
from app.discovery import derivation_rules as rules_module
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.model.bridge_notebook import TARGET_SCHEMA, build_notebook, dedup_select, target_table
from app.model.recon_sql import check_sql
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
NR, MON = t503.NR, t503.MON
SANDBOX = t504.SANDBOX
BRIDGE = "nexora_test.bridge_product_promotion"
BRIDGE_SPARK = f"{TARGET_SCHEMA}.bridge_b1_product_promotion"
DERIVED_SQL = (
    "SELECT p.`ProductID` AS `ProductID`, m.`PromotionID` AS `PromotionID` FROM `saleslt`.`SalesLT_Product` p "
    "JOIN `nexora_test`.`dim_promotion` m ON m.`PromotionName` = p.`Name`"
)


def evidence(i, from_col, to_table, to_col):
    return RelationshipEvidence(
        id=f"R{i}", **{"from": ColumnRef(table=BRIDGE, column=from_col)}, to=ColumnRef(table=to_table, column=to_col), source="heuristic",
        type_compatible=True, overlap_pct=100.0, orphan_rate=0.0, null_rate=0.0, cardinality="many_to_one", confidence="high",
        evidence=RelationshipQueryEvidence(computed_at=utc_now()),
    )


def spark_columns(*names):
    return [InventoryColumn(name=n, data_type="INT" if n.endswith("ID") else "STRING") for n in names]


def base_spec(run_id, workspace_ok=True):
    spec = t503.spec_with_measures(run_id=run_id)
    spec.tables.append(
        ModelTable(
            name="Promotion", source=TableSource(schema="nexora_test", table="dim_promotion"), role="dimension",
            columns=[ModelColumn(name="PromotionID", source_column="PromotionID", data_type="int64"), ModelColumn(name="PromotionName", source_column="PromotionName", data_type="string")],
        )
    )
    return spec


class BridgeItems(t504.FakeItems):
    def __init__(self):
        super().__init__()
        self.notebooks, self.runs, self.run_error = [], [], None

    def upsert_notebook(self, workspace_id, display_name, ipynb):
        self.notebooks.append((workspace_id, display_name, Path(ipynb), Path(ipynb).read_text(encoding="utf-8")))
        return {"item_id": "nb-1", "operation": "created"}

    def run_notebook(self, workspace_id, item_id, timeout_s=900):
        self.runs.append((workspace_id, item_id))
        if self.run_error:
            raise self.run_error
        return {"run_id": "job-7"}


class BridgePbi(t504.FakePbi):
    """Doubles the totals when the deployed model has the bridge, if asked to (double counting)."""

    def __init__(self, items):
        super().__init__()
        self.items, self.double = items, False

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        rows = super().execute_dax(workspace_id, dataset_id, dax, impersonate)
        folder = Path(self.items.calls[-1][2])
        if self.double and any(folder.glob("definition/tables/bridge*.tmdl")) and "CALCULATE" not in dax and "COUNTROWS" not in dax:
            return [{k: (v * 2 if isinstance(v, (int, float)) else v) for k, v in rows[0].items()}]
        return rows


class BridgeSql(t504.FakeSql):
    def __init__(self):
        super().__init__()
        self.bridge_rows, self.bridge_dupes, self.bridge_orphans = 16, 0, 0

    def answer(self, statement):
        if "bridge" in statement.lower():
            if "HAVING COUNT_BIG(*) > 1) AS d" in statement:
                return [(self.bridge_dupes,)]
            if "TOP 5" in statement:
                return [(7, 2, 2)] * min(self.bridge_dupes, 5)
            if statement.startswith("SELECT COUNT_BIG(*) FROM"):
                return [(self.bridge_rows,)]
            if "LEFT JOIN (SELECT DISTINCT" in statement and "AS d ON s." in statement:
                return [(self.bridge_rows, self.bridge_orphans)]
        return super().answer(statement)


class FakeLakehouse:
    def __init__(self):
        self.tables: dict[str, list[tuple[str, str]]] = {}

    def list_tables(self, schema):
        return [{"name": n} for n in self.tables if self.schema_of.get(n) == schema]

    schema_of: dict[str, str] = {}

    def get_table_metadata(self, schema, name):
        return {"columns": [{"name": c, "type_name": t, "nullable": True} for c, t in self.tables[name]]}


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX

    # --- the notebook ----------------------------------------------------------------------------
    lakehouse = {"id": t502.LH, "name": "SalesLakehouse", "workspace_id": SANDBOX}
    nb = build_notebook("B1", dedup_select("nexora_test", "bridge_product_promotion", ("ProductID", "PromotionID")), "bridge_b1_x", "Removes duplicates.", lakehouse)
    text = json.dumps(nb)
    check("notebook: nbformat 4 with the Lakehouse attached as the default", nb["nbformat"] == 4 and nb["metadata"]["dependencies"]["lakehouse"] == {
        "default_lakehouse": t502.LH, "default_lakehouse_name": "SalesLakehouse", "default_lakehouse_workspace_id": SANDBOX})
    check("notebook: creates only its table in nexora_generated, from a SELECT DISTINCT, and fails when empty",
          "CREATE SCHEMA IF NOT EXISTS `nexora_generated`" in text and "CREATE OR REPLACE TABLE `nexora_generated`.`bridge_b1_x`" in text and "SELECT DISTINCT `ProductID`, `PromotionID`" in text
          and "assert rows > 0" in text and text.count("CREATE OR REPLACE TABLE") == 1 and "DROP" not in text and "INSERT" not in text)
    check("notebook: deterministic", build_notebook("B1", "SELECT 1", "t", "d", lakehouse) == build_notebook("B1", "SELECT 1", "t", "d", lakehouse))
    check("notebook: a name that is safe and stable", target_table("B1", "Product_Promotion") == "bridge_b1_product_promotion" and target_table("B-2", "a b") == "bridge_b_2_a_b")
    check("sql guard: Spark backticks are read", check_sql(DERIVED_SQL, t502.build_inventory(base_spec("r")), bc.Kpi(name="DR1", formula_intent="x"), "q") == [])

    # --- the items client: notebooks and jobs --------------------------------------------------------
    class Resp:
        def __init__(self, status=200, body=None, headers=None):
            self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}

        def json(self):
            return self._body

    sent, queue = [], []

    def http(method, url, headers=None, json=None, params=None, timeout=None):
        sent.append((method, url, json))
        return queue.pop(0)

    clock = {"t": 0.0}
    client_f = FabricItemsClient(token_provider=lambda: "t", http=http, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        path.write_text("{}", encoding="utf-8")
        queue[:] = [Resp(200, {"value": []}), Resp(201, {"id": "nb-9"})]
        result = client_f.upsert_notebook(SANDBOX, "Nexora_B1", path)
        body = sent[-1][2]
        check("notebook upsert: POST /notebooks with the ipynb definition", result == {"item_id": "nb-9", "operation": "created"} and sent[-1][1].endswith("/notebooks")
              and body["definition"]["format"] == "ipynb" and body["definition"]["parts"][0]["path"] == "notebook-content.ipynb")
        queue[:] = [Resp(200, {"value": [{"id": "nb-9", "displayName": "nexora_b1", "type": "Notebook"}]}), Resp(200, {})]
        check("notebook upsert: an existing notebook is updated, not duplicated", client_f.upsert_notebook(SANDBOX, "Nexora_B1", path) == {"item_id": "nb-9", "operation": "updated"})
        check("notebook upsert: only the sandbox", _raises(TargetNotAllowed, client_f.upsert_notebook, "99999999-9999-4999-8999-999999999999", "x", path))
    sent.clear()
    loc = "https://api.fabric.microsoft.com/v1/workspaces/w/items/nb-9/jobs/instances/job-42"
    queue[:] = [Resp(202, {}, {"Location": loc}), Resp(200, {"status": "InProgress"}, {"Retry-After": "4"}), Resp(200, {"id": "job-42", "status": "Completed"})]
    check("run_notebook: POSTs the job, polls the instance, returns the run id", client_f.run_notebook(SANDBOX, "nb-9") == {"run_id": "job-42"}
          and sent[0][1].endswith("/items/nb-9/jobs/instances?jobType=RunNotebook") and sent[1][1] == loc)
    queue[:] = [Resp(202, {}, {"Location": loc}), Resp(200, {"id": "job-42", "status": "Failed", "failureReason": {"message": "AssertionError: empty bridge"}}, {"RequestId": "r-1"})]
    err = _raises(NotebookRunFailed, client_f.run_notebook, SANDBOX, "nb-9")
    check("run_notebook: a failed run names the run id and Fabric's reason", err and err.run_id == "job-42" and "empty bridge" in str(err) and err.request_id == "r-1")

    # --- the endpoint -----------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        rules_file = Path(tmp) / "rules.yaml"
        rules_file.write_text(
            "rules:\n  - id: DR1\n    entities: [Product, Promotion]\n    description: A product is in a promotion when its name equals the promotion name\n    approved_by: o@contoso.com\n"
            "  - id: DR2\n    entities: [Product, Promotion]\n    description: Keep one row per product and promotion pair\n    approved_by: o@contoso.com\n    deduplicate: true\n",
            encoding="utf-8",
        )
        original_rules = rules_module.CONFIG_PATH
        rules_module.CONFIG_PATH = rules_file
        store = FileRunStore(runs_root)
        items = BridgeItems()
        pbi = BridgePbi(items)
        sql = BridgeSql()
        lake = FakeLakehouse()
        recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
        dax_llm = t503.FakeLlm()
        bridge_llm_calls = []
        bridge_answers = [json.dumps({"select_sql": DERIVED_SQL, "explanation": "ok"})]

        def bridge_llm(system, user, schema):
            bridge_llm_calls.append(user)
            return bridge_answers[min(len(bridge_llm_calls) - 1, len(bridge_answers) - 1)]

        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        app.dependency_overrides[get_dax_llm] = lambda: dax_llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: lake)
        app.dependency_overrides[get_recon_llm] = lambda: recon
        app.dependency_overrides[get_bridge_llm] = lambda: bridge_llm
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def reset():
            items.error, items.run_error = None, None
            items.notebooks.clear(), items.runs.clear()
            pbi.double, pbi.errors = False, {}
            sql.bridge_rows, sql.bridge_dupes, sql.bridge_orphans, sql.total = 16, 0, 0, 1000.0
            lake.tables.clear()
            lake.schema_of = {}
            bridge_llm_calls.clear()
            dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}

        def new_run(situation="existing_table", status="approved", duplicates=0, dedup_rule=None, rule="DR1", scope_workspace=SANDBOX, validate=True):
            run = store.create("Show net revenue by product and promotion")
            spec = base_spec(run.run_id)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = t502.build_plan(spec)
            save_yaml(spec, run_dir / "ModelSpec.yaml")
            save_yaml(plan, run_dir / "ExecutionPlan.yaml")
            inventory = t502.build_inventory(spec)
            inventory.tables.append(InventoryTable(schema="nexora_test", table_name="bridge_product_promotion", columns=spark_columns("ProductID", "PromotionID")))
            inventory.relationship_evidence = [evidence(1, "ProductID", "saleslt.SalesLT_Product", "ProductID"), evidence(2, "PromotionID", "nexora_test.dim_promotion", "PromotionID")]
            write_json(run_dir / "MetadataInventory.json", inventory.to_dict())
            resolution = SourceResolution(
                run_id=run.run_id, version=1,
                entities=[
                    EntityResolution(entity="Product", selected=SelectedSource(table="saleslt.SalesLT_Product", score=1.0, conformed=True, rationale="r")),
                    EntityResolution(entity="Promotion", selected=SelectedSource(table="nexora_test.dim_promotion", score=1.0, conformed=True, rationale="r")),
                ],
            )
            write_json(run_dir / "SourceResolution.json", resolution.to_dict())
            proposal = BridgeProposal(
                id="B1", entities=["Product", "Promotion"], situation=situation, status=status,
                table=BRIDGE if situation == "existing_table" else None, derivation_rule=rule if situation == "derivation_rule" else None,
                deduplication_rule=dedup_rule, evidence={"duplicate_pairs": duplicates} if situation == "existing_table" else {},
            )
            write_json(run_dir / "BridgeProposals.json", BridgeProposals(run_id=run.run_id, version=1, proposals=[proposal]).to_dict())
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Revenue", entities=["Sales", "Product", "Promotion"], kpis=[t503.KPI_NR, t503.KPI_MON]), run_dir / "BusinessContract.yaml")
            run.scope = RunScope(workspace_id=scope_workspace, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
            for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
                run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.artifacts["MetadataInventory"] = ArtifactRef(path="MetadataInventory.json", version=1, hash=canonical_hash(inventory.to_dict()), status="approved", updated_at=utc_now())
            run.state = RunState.PLAN_APPROVED
            store.save(run)
            assert client.post(f"/runs/{run.run_id}/model/generate").status_code == 200
            reset()
            assert client.post(f"/runs/{run.run_id}/model/measures").status_code == 200
            if validate:
                assert client.post(f"/runs/{run.run_id}/model/validate").json()["report"]["passed"] is True
            return run.run_id

        def generate(rid, bridge="B1"):
            return client.post(f"/runs/{rid}/model/bridges/{bridge}/generate")

        def by(r):
            return {(c["id"], c["object"]): c for c in r.json()["validation"]["checks"]}

        # 1. An existing mapping table
        rid = new_run()
        r = generate(rid)
        body = r.json()
        check("existing table: 200, version 3, every G4 check passes", r.status_code == 200 and body["version"] == 3 and body["validation"]["passed"] is True
              and not [c for c in body["validation"]["checks"] if c["status"] == "fail"])
        ids = {c["id"] for c in body["validation"]["checks"]}
        check("the G4.BRIDGE.* checks ran", {"G4.BRIDGE.UNIQUE", "G4.BRIDGE.ORPHANS", "G4.BRIDGE.TOTALS"} <= ids
              and len([c for c in body["validation"]["checks"] if c["id"] == "G4.BRIDGE.ORPHANS"]) == 2)
        spec = load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml")
        bridge_table = next(t for t in spec.tables if t.role == "bridge")
        rels = {r_.to.table: r_ for r_ in spec.relationships if r_.from_.table == bridge_table.name}
        check("the mapping table is in the model as a bridge with two many-to-one relationships to the dimensions",
              bridge_table.name == "bridge_product_promotion" and set(rels) == {"Product", "Promotion"} and all(r_.cardinality == "many_to_one" for r_ in rels.values())
              and rels["Product"].evidence_ref == "R1" and rels["Promotion"].evidence_ref == "R2")
        check("filter directions: both toward the dimension that reaches the facts, single toward the other",
              rels["Product"].cross_filter == "both" and rels["Promotion"].cross_filter == "single")
        check("the bridge's key columns are hidden", all(c.is_hidden for c in bridge_table.columns))
        rel_text = (runs_root / rid / "model" / "v3" / "definition" / "relationships.tmdl").read_text(encoding="utf-8")
        check("TMDL v3 has the bridge table and its relationships", (runs_root / rid / "model" / "v3" / "definition" / "tables" / "bridge_product_promotion.tmdl").is_file()
              and "fromColumn: bridge_product_promotion.ProductID" in rel_text and "crossFilteringBehavior: bothDirections" in rel_text)
        totals = by(r)[("G4.BRIDGE.TOTALS", f"measure:{NR}")]
        check("G4.BRIDGE.TOTALS shows the totals before and with the bridge", totals["status"] == "pass" and totals["evidence"]["total_before"] == 1000.0 and totals["evidence"]["total_with_bridge"] == 1000.0)
        check("no notebook for a clean existing table", not items.notebooks and not items.runs)
        run = store.get(rid)
        check("artifacts follow: TMDLModel v3, ModelSpec v3, state stays MODEL_GENERATING", run.artifacts["TMDLModel"].version == 3 and run.artifacts["ModelSpec"].version == 3 and run.state is RunState.MODEL_GENERATING)
        check("an earlier validation is the baseline: v2 is untouched", (runs_root / rid / "model" / "v2").is_dir())

        # 2. Not allowed
        reset()
        check("a proposal that isn't approved: 409 BRIDGE_NOT_APPROVED", envelope(generate(new_run(status="proposed")), 409, "BRIDGE_NOT_APPROVED"))
        rid = new_run()
        check("an unknown bridge: 404", envelope(generate(rid, "B9"), 404, "NOT_FOUND"))
        p = runs_root / rid / "BridgeProposals.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["proposals"][0]["situation"] = "needs_rules"
        p.write_text(json.dumps(data), encoding="utf-8")
        check("needs_rules can never be generated: 409 MAPPING_RULES_REQUIRED", envelope(generate(rid), 409, "MAPPING_RULES_REQUIRED"))
        rid = new_run()
        run = store.get(rid)
        run.state = RunState.MODEL_FAILED
        store.save(run)
        check("wrong state: 409", envelope(generate(rid), 409, "WRONG_STATE"))
        check("duplicate pairs and no deduplication rule: 409 MAPPING_RULES_REQUIRED", envelope(generate(new_run(duplicates=3)), 409, "MAPPING_RULES_REQUIRED"))

        # 3. Duplicates in the table fail validation
        rid = new_run()
        sql.bridge_dupes = 3
        r = generate(rid)
        c = by(r)[("G4.BRIDGE.UNIQUE", "bridge:bridge_product_promotion")]
        check("duplicate pairs fail G4.BRIDGE.UNIQUE, showing the pair", c["status"] == "fail" and c["code"] == "BRIDGE_DUPLICATE_PAIRS" and "(7, 2)" in c["message"]
              and c["evidence"]["duplicates"][0] == {"pair": [7, 2], "count": 2} and r.json()["validation"]["passed"] is False and store.get(rid).state is RunState.MODEL_FAILED)
        sql.bridge_dupes = 0

        # 4. An empty bridge, orphans, double counting
        rid = new_run()
        sql.bridge_rows = 0
        c = by(generate(rid))[("G4.BRIDGE.UNIQUE", "bridge:bridge_product_promotion")]
        check("an empty bridge fails with 'empty bridge'", c["status"] == "fail" and "empty bridge" in c["message"] and c["code"] == "BRIDGE_EMPTY")
        rid = new_run()
        sql.bridge_orphans = 8
        r = generate(rid)
        orphan = [c for c in r.json()["validation"]["checks"] if c["id"] == "G4.BRIDGE.ORPHANS"]
        check("orphan keys above the threshold fail G4.BRIDGE.ORPHANS", all(c["status"] == "fail" and c["code"] == "BRIDGE_ORPHANS" for c in orphan) and store.get(rid).state is RunState.MODEL_FAILED)
        rid = new_run()
        sql.bridge_orphans = 1
        r = generate(rid)
        check("a few orphan keys are only a warning", r.json()["validation"]["passed"] is True and all(c["status"] == "warning" for c in r.json()["validation"]["checks"] if c["id"] == "G4.BRIDGE.ORPHANS"))
        sql.bridge_orphans = 0
        rid = new_run()
        pbi.double = True
        r = generate(rid)
        c = by(r)[("G4.BRIDGE.TOTALS", f"measure:{NR}")]
        check("a grand total that changes once the bridge is added fails G4.BRIDGE.TOTALS", c["status"] == "fail" and c["code"] == "BRIDGE_TOTALS_CHANGED" and c["evidence"]["total_before"] == 1000.0
              and c["evidence"]["total_with_bridge"] == 2000.0 and "double counting" in c["message"] and store.get(rid).state is RunState.MODEL_FAILED)
        pbi.double = False
        rid = new_run(validate=False)
        c = by(generate(rid))[("G4.BRIDGE.TOTALS", "model")]
        check("without an earlier validated version the totals can only warn", c["status"] == "warning" and c["code"] == "NO_BASELINE")

        # 5. Duplicate pairs under an approved deduplication rule: a deterministic notebook
        rid = new_run(duplicates=3, dedup_rule="DR2")
        lake.tables["bridge_b1_bridge_product_promotion"] = [("ProductID", "INT"), ("PromotionID", "INT")]
        lake.schema_of = {"bridge_b1_bridge_product_promotion": TARGET_SCHEMA}
        r = generate(rid)
        check("deduplication: 200 and a passing validation", r.status_code == 200 and r.json()["validation"]["passed"] is True)
        ws, name, path, nb_text = items.notebooks[0]
        check("the notebook was deployed to the sandbox, then run", ws == SANDBOX and name == f"Nexora_{rid[:8]}_B1" and path == runs_root / rid / "bridges" / "B1.ipynb" and items.runs == [(SANDBOX, "nb-1")])
        check("the notebook is a SELECT DISTINCT of the mapping table, in nexora_generated, on the sandbox Lakehouse",
              "SELECT DISTINCT `ProductID`, `PromotionID` FROM `nexora_test`.`bridge_product_promotion`" in nb_text and "`nexora_generated`.`bridge_b1_bridge_product_promotion`" in nb_text
              and f'"default_lakehouse_workspace_id": "{SANDBOX}"' in nb_text and not bridge_llm_calls)
        spec = load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml")
        table = next(t for t in spec.tables if t.role == "bridge")
        check("the model references the deduplicated copy, with the evidence of the mapping table",
              table.source.schema_name == TARGET_SCHEMA and table.source.table == "bridge_b1_bridge_product_promotion"
              and {r_.evidence_ref for r_ in spec.relationships if r_.from_.table == table.name} == {"R1", "R2"})
        inventory = json.loads((runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))
        check("the new table was harvested into the inventory", any(t["schema"] == TARGET_SCHEMA and t["table_name"] == "bridge_b1_bridge_product_promotion" for t in inventory["tables"])
              and TARGET_SCHEMA in inventory["scope"]["schemas"])

        # 6. A derivation rule: the model writes the SQL
        reset()
        rid = new_run(situation="derivation_rule", rule="DR1")
        lake.tables["bridge_b1_product_promotion"] = [("ProductID", "INT"), ("PromotionID", "INT")]
        lake.schema_of = {"bridge_b1_product_promotion": TARGET_SCHEMA}
        r = generate(rid)
        check("derivation rule: 200 and a passing validation", r.status_code == 200 and r.json()["validation"]["passed"] is True)
        check("the model saw the rule and names only, and its SQL is in the notebook",
              "A product is in a promotion" in bridge_llm_calls[0] and set(json.loads(bridge_llm_calls[0])) == {"rule", "entity_tables", "output_columns", "tables"}
              and all(set(t) == {"table", "columns"} and all(set(c) == {"name", "data_type"} for c in t["columns"]) for t in json.loads(bridge_llm_calls[0])["tables"])
              and DERIVED_SQL.splitlines()[0] in items.notebooks[0][3])
        spec = load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml")
        table = next(t for t in spec.tables if t.role == "bridge")
        check("the relationships say they come from the approved rule",
              table.source.table == "bridge_b1_product_promotion" and all("derivation rule DR1" in (r_.override_rationale or "") and r_.evidence_ref is None for r_ in spec.relationships if r_.from_.table == table.name))
        calls = len(bridge_llm_calls)
        check("a repeat call replays the saved SQL: no model call", generate(rid).status_code == 200 and len(bridge_llm_calls) == calls)

        reset()
        rid = new_run(situation="derivation_rule", rule="DR1")
        bridge_answers[:] = [json.dumps({"select_sql": DERIVED_SQL + " WHERE p.`ListPrice` > 99", "explanation": ""})]
        r = generate(rid)
        check("SQL with an unstated literal is refused (2 attempts), and nothing is written",
              envelope(r, 422, "VALIDATION_ERROR") and "hardcodes 99" in r.json()["error"]["message"] and len(bridge_llm_calls) == 2 and not items.notebooks
              and load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").version == 2)
        bridge_answers[:] = [json.dumps({"select_sql": "DROP TABLE saleslt.SalesLT_Product", "explanation": ""})]
        check("SQL that isn't a SELECT is refused", envelope(generate(rid), 422, "VALIDATION_ERROR") and not items.notebooks)
        bridge_answers[:] = [json.dumps({"select_sql": DERIVED_SQL, "explanation": "ok"})]

        # 7. Failures
        reset()
        rid = new_run(situation="derivation_rule", rule="DR1")
        items.run_error = NotebookRunFailed("The notebook run job-9 failed: Py4JJavaError", "req-5", {"message": "Py4JJavaError"}, "job-9")
        r = generate(rid)
        check("a failing notebook is an upstream error naming the run, and the model is unchanged",
              envelope(r, 502, "UPSTREAM_ERROR") and "job-9" in r.json()["error"]["message"] and load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").version == 2
              and not (runs_root / rid / "model" / "v3").exists() and store.get(rid).state is RunState.MODEL_GENERATING)
        reset()
        rid = new_run(situation="derivation_rule", rule="DR1")
        lake.tables["bridge_b1_product_promotion"] = [("ProductID", "INT"), ("Other", "INT")]
        lake.schema_of = {"bridge_b1_product_promotion": TARGET_SCHEMA}
        r = generate(rid)
        check("a derived table without the expected columns is refused", envelope(r, 422, "VALIDATION_ERROR") and "PromotionID" in r.json()["error"]["message"] and not (runs_root / rid / "model" / "v3").exists())
        reset()
        rid = new_run(situation="derivation_rule", rule="DR1", scope_workspace=t502.WS)
        check("a Lakehouse outside the sandbox workspace is refused before any notebook is deployed", envelope(generate(rid), 403, "TARGET_NOT_ALLOWED") and not items.notebooks)
        reset()
        check("unknown run: 404", envelope(generate("nope"), 404, "RUN_NOT_FOUND"))

        rules_module.CONFIG_PATH = original_rules
        app.dependency_overrides.clear()

    print("All checks passed.")


def _raises(exc, fn, *args):
    try:
        fn(*args)
    except exc as e:
        return e
    return None


if __name__ == "__main__":
    main()
