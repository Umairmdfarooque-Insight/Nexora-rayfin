"""T-404 offline checks: ExecutionPlan and ModelSpec generation, validation, the plan endpoints and
`python -m app.cli plan validate`.

Run: python -m app.manual_tests.t404_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse, SQL endpoint and model are fakes, and roles, conformed sources, the glossary and
the derivation rules are temporary files (extending the fixtures of t302 to t403), so no sign-in
or network is needed. The live steps are in MANUAL_TESTING.md.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import cli
from app.api.deps import (
    get_connector_factory,
    get_feasibility_llm,
    get_relationship_llm,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.config import PROJECT_ROOT
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, RlsRole, load_yaml, save_yaml
from app.discovery import derivation_rules as rules_module
from app.discovery import glossary as glossary_module
from app.manual_tests import t302_offline_checks as base
from app.manual_tests import t303_offline_checks as t303  # extends the t302 fixtures
from app.manual_tests import t401_offline_checks as t401  # extends them further
from app.manual_tests import t402_offline_checks as t402  # adds the date column
from app.manual_tests import t403_offline_checks as t403  # the bridge fixtures and fake SQL
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
DATE_TABLE = "nexora_test.dim_date"
NR, MON, RET = t402.NR, t402.MON, t402.RET

# --- fixtures on top of t402: a date table and a Detail to Header key --------------------------
base.LAKEHOUSE["nexora_test"]["dim_date"] = [col("Date", "DATE"), col("Year", "INT"), col("Quarter", "STRING"), col("MonthNumber", "INT"), col("MonthName", "STRING")]
base.DATA[("nexora_test", "dim_date")] = {
    "Date": [base.D(2024, 1, 1), base.D(2024, 1, 2)],
    "Year": [2024, 2024],
    "Quarter": ["Q1", "Q1"],
    "MonthNumber": [1, 1],
    "MonthName": ["January", "January"],
}
base.LAKEHOUSE["saleslt"]["SalesLT_SalesOrderDetail"].append(col("SalesOrderID", "INT"))
base.DATA[("saleslt", "SalesLT_SalesOrderDetail")]["SalesOrderID"] = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]

DETAIL_TO_HEADER = {"from": {"table": D, "column": "SalesOrderID"}, "to": {"table": H, "column": "SalesOrderID"}}
HEADER_TO_DATE = {"from": {"table": H, "column": "OrderDate"}, "to": {"table": DATE_TABLE, "column": "Date"}}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = t403.BridgeSql()
        roles_file, conformed_file, glossary_file, rules_file = (tmp_dir / n for n in ("roles.yaml", "conformed.yaml", "glossary.yaml", "rules.yaml"))
        t401.write_roles(roles_file)
        t401.write_conformed(conformed_file, [P, C, D, H, DATE_TABLE])
        glossary_file.write_text(
            "terms:\n  product: [product, products]\n  customer: [customer, customers]\n  sales: [sales, salesorderdetail]\n",
            encoding="utf-8",
        )
        rules_file.write_text("rules: []\n", encoding="utf-8")
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = (
            roles_file, conformed_file, glossary_file, rules_file,
        )

        source_llm = t401.FakeSourceLlm(t402.SOURCE_ANSWER)
        feasibility_llm = t402.FakeFeasibilityLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=UPN, name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: base.FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_source_llm] = lambda: source_llm
        app.dependency_overrides[get_feasibility_llm] = lambda: feasibility_llm
        app.dependency_overrides[get_relationship_llm] = lambda: (lambda s, u, sc: json.dumps({"relationships": []}))
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(NR, MON), schemas=("saleslt", "nexora_test"), security=(), feasibility=True, bridges=False):
            run = store.create("Show net revenue by product and customer")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=list(schemas),
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"),
            )
            run.state = RunState.REQUIREMENT_APPROVED
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            contract = bc.BusinessContract(
                run_id=run.run_id,
                business_objective="Revenue",
                entities=["Sales", "Product", "Customer"],
                kpis=[bc.Kpi(name=k, grain="order line", currency="USD", **t402.KPIS[k]) for k in kpis],
                security=[bc.SecurityRequirement(description=d, applies_to=["Customer"]) for d in security],
            )
            bc.save(contract, run_dir / "BusinessContract.yaml")
            assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
            candidates = [DETAIL_TO_HEADER] + ([HEADER_TO_DATE] if "nexora_test" in schemas else [])
            assert client.post(f"/runs/{run.run_id}/relationships/analyze", json={"candidates": candidates}).status_code == 200
            run = store.get(run.run_id)
            run.state = RunState.DATA_PROFILED
            store.save(run)
            assert client.post(f"/runs/{run.run_id}/plan/sources").status_code == 200
            if feasibility:
                feasibility_llm.answers = [t402.default_answer()]
                assert client.post(f"/runs/{run.run_id}/plan/feasibility").status_code == 200
                run = store.get(run.run_id)
                run.state = RunState.DATA_PROFILED  # a data-gap KPI parks the run; resume it for these checks
                run.resume_state = None
                store.save(run)
            if bridges:
                assert client.post(f"/runs/{run.run_id}/plan/bridges").status_code == 200
            return run.run_id

        def plan(rid):
            return client.post(f"/runs/{rid}/plan")

        def checks_of(body):
            return {c["id"]: c for c in body["validation"]["checks"]}

        def cli_validate(rid):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli._plan_command(cli._build_parser().parse_args(["plan", "validate", str(runs_root / rid)]))
            return code, buffer.getvalue()

        def edit_model(rid, change):
            path = runs_root / rid / "ModelSpec.yaml"
            model = load_yaml(ModelSpec, path)
            change(model)
            save_yaml(model, path)

        # AC1: plan generated
        rid = new_run()
        r = plan(rid)
        body = r.json()
        spec, execution = body["model_spec"], body["execution_plan"]
        tables = {t["name"]: t for t in spec["tables"]}
        check("plan generated: 200 and both YAML files exist", r.status_code == 200 and (runs_root / rid / "ModelSpec.yaml").is_file() and (runs_root / rid / "ExecutionPlan.yaml").is_file())
        check("ModelSpec header: version 1, Direct Lake, model name from the run id", spec["version"] == 1 and spec["storage_mode"] == "DirectLake" and spec["model_name"] == f"Nexora_{rid[:4]}"
              and set(spec["based_on"]) == {"MetadataInventory", "SourceResolution"})
        check("the Sales fact is at grain 'order line' and maps SalesOrderDetail", tables["Sales"]["role"] == "fact" and tables["Sales"]["grain"] == "order line"
              and tables["Sales"]["source"] == {"schema": "saleslt", "table": "SalesLT_SalesOrderDetail"})
        check("Product and Customer are dimensions, the date table has role date", tables["Product"]["role"] == "dimension" and tables["Customer"]["role"] == "dimension" and tables["Date"]["role"] == "date"
              and tables["Date"]["source"] == {"schema": "nexora_test", "table": "dim_date"})
        pairs = {(r_["from"]["table"], r_["to"]["table"]) for r_ in spec["relationships"]}
        check("relationships reach Product, Customer (through the order header) and Date", {("Sales", "Product"), ("Sales", "SalesLT_SalesOrderHeader"), ("SalesLT_SalesOrderHeader", "Customer"), ("SalesLT_SalesOrderHeader", "Date")} == pairs)
        check("every relationship has an evidence_ref and a measured cardinality", all(r_["evidence_ref"] and r_["cardinality"] in ("many_to_one", "one_to_one") and r_["cross_filter"] == "single" for r_ in spec["relationships"]))
        evidence = {e["id"]: e for e in json.loads((runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))["relationship_evidence"]}
        check("each evidence_ref is a high or medium evidence in the inventory", all(evidence[r_["evidence_ref"]]["confidence"] in ("high", "medium") for r_ in spec["relationships"]))
        check("Date has a Calendar hierarchy: Year, Quarter, Month on MonthName", tables["Date"]["hierarchies"] == [{"name": "Calendar", "levels": [
            {"name": "Year", "column": "Year"}, {"name": "Quarter", "column": "Quarter"}, {"name": "Month", "column": "MonthName"}]}])
        measures = {m["name"]: m for m in spec["measures"]}
        check("measures: Net Revenue on Sales with formula intent only and dax null", measures[NR]["table"] == "Sales" and measures[NR]["dax"] is None and measures[NR]["kpi"] == NR
              and measures[NR]["format_string"] == "$#,0" and measures[NR]["description"].startswith("Approved: Sum of line totals") and set(measures) == {NR, MON})
        check("the foreign keys and measure source columns are hidden", {c["name"]: c["is_hidden"] for c in tables["Sales"]["columns"]}["LineTotal"] is True
              and {c["name"]: c["is_hidden"] for c in tables["Sales"]["columns"]}["ProductID"] is True)
        check("binary columns the model can't hold are left out", "Thumbnail" not in {c["name"] for c in tables["Product"]["columns"]})
        check("no inferred security: roles is []", spec["roles"] == [])
        check("ExecutionPlan: sources, stages, target and approvals", {s["table"] for s in execution["sources"]} == {D, P, C, H, DATE_TABLE}
              and all(s["conformed"] and s["exception_id"] is None for s in execution["sources"]) and [s["name"] for s in execution["stages"]] == ["semantic_model", "report", "deployment"]
              and execution["target"] == {"workspace_id": WS, "kind": "sandbox"} and execution["approvals_required"] == [{"gate": "G3", "roles": ["bi_engineer"]}, {"gate": "G4", "roles": ["bi_engineer", "business_owner"]}]
              and execution["bridges"] == [] and execution["data_engineering_actions"] == [] and execution["feasibility_ref"] == "Feasibility.json")
        run = store.get(rid)
        check("both artifacts are registered; the plan is based on the model's hash", run.artifacts["ModelSpec"].version == 1 and run.artifacts["ExecutionPlan"].version == 1
              and run.artifacts["ExecutionPlan"].based_on == {"ModelSpec": run.artifacts["ModelSpec"].hash} and run.state is RunState.DATA_PROFILED)
        check("a clean plan validates", body["validation"]["gate"] == "PLAN" and body["validation"]["passed"] is True and all(c["status"] == "pass" for c in body["validation"]["checks"]))
        check("the file is YAML with LF and no DAX", b"\r\n" not in (runs_root / rid / "ModelSpec.yaml").read_bytes() and "dax: null" in (runs_root / rid / "ModelSpec.yaml").read_text(encoding="utf-8"))

        first = ((runs_root / rid / "ModelSpec.yaml").read_bytes(), (runs_root / rid / "ExecutionPlan.yaml").read_bytes())
        r = plan(rid)
        check("a second call gives identical files and versions", r.status_code == 200 and first == ((runs_root / rid / "ModelSpec.yaml").read_bytes(), (runs_root / rid / "ExecutionPlan.yaml").read_bytes())
              and store.get(rid).artifacts["ModelSpec"].version == 1)
        g = client.get(f"/runs/{rid}/plan")
        check("GET /plan returns the same files", g.status_code == 200 and g.json()["model_spec"] == spec and g.json()["execution_plan"] == execution)

        # AC2: bad reference caught
        code, output = cli_validate(rid)
        check("the CLI says VALID for the clean plan", code == 0 and output.strip() == "VALID")
        completed = subprocess.run([sys.executable, "-m", "app.cli", "plan", "validate", str(runs_root / rid)], capture_output=True, text=True, cwd=PROJECT_ROOT)
        check("python -m app.cli plan validate works as a command", completed.returncode == 0 and completed.stdout.strip() == "VALID")

        def break_column(model):
            next(x for x in model.relationships if x.name == "Sales_Product").to.column = "NoSuchColumn"

        edit_model(rid, break_column)
        code, output = cli_validate(rid)
        check("a relationship to Product.NoSuchColumn fails, naming the path", code == 1 and output.startswith("INVALID") and "PLAN.1: relationships[Sales_Product].to.column: Product.NoSuchColumn" in output)
        g = client.get(f"/runs/{rid}/plan")
        check("GET /plan shows the same failure", g.json()["validation"]["passed"] is False and "relationships[Sales_Product].to.column: Product.NoSuchColumn" in checks_of(g.json())["PLAN.1"]["evidence"]["paths"])

        def break_table(model):
            model.tables[0].source.table = "Gone"

        edit_model(rid, break_table)
        code, output = cli_validate(rid)
        check("a table that isn't in the inventory fails", code == 1 and "tables[Sales].source: saleslt.Gone is not in the inventory" in output)
        r = plan(rid)
        check("regenerating restores a valid plan as a new version", r.status_code == 200 and r.json()["validation"]["passed"] is True and store.get(rid).artifacts["ModelSpec"].version == 2
              and cli_validate(rid)[0] == 0)

        (runs_root / rid / "ModelSpec.yaml").write_text((runs_root / rid / "ModelSpec.yaml").read_text(encoding="utf-8").replace("storage_mode:", "storage_modes:"), encoding="utf-8")
        code, output = cli_validate(rid)
        check("an unknown key in the YAML is INVALID with its path", code == 1 and "storage_modes" in output)
        plan(rid)  # restore
        check("a missing file is exit 2", cli._plan_command(cli._build_parser().parse_args(["plan", "validate", str(runs_root / RANDOM)])) == 2)

        # policy checks
        def weak_evidence(model):
            low = next(i for i, e in evidence.items() if e["confidence"] == "low" and e["status"] == "candidate")
            model.relationships[0].evidence_ref = low

        edit_model(rid, weak_evidence)
        code, output = cli_validate(rid)
        check("a low-confidence relationship without an override fails, naming it", code == 1 and "PLAN.2: relationships[Sales_Product]" in output and "low" in output)

        def override(model):
            model.relationships[0].override_rationale = "Confirmed with the data owner"

        edit_model(rid, override)
        check("an explicit override with a rationale passes", cli_validate(rid)[0] == 0)

        def no_ref(model):
            model.relationships[0].evidence_ref = None
            model.relationships[0].override_rationale = None

        edit_model(rid, no_ref)
        check("no evidence_ref and no override fails", "no evidence_ref and no override" in cli_validate(rid)[1])
        plan(rid)

        def twice(model):
            model.relationships.append(model.relationships[0].model_copy(update={"name": "Sales_Product_2"}))

        edit_model(rid, twice)
        check("two relationships between the same tables fail", "more than one relationship between the same two tables" in cli_validate(rid)[1])
        plan(rid)

        def calculated(model):
            model.tables[-1].source = None

        edit_model(rid, calculated)
        check("a calculated table fails under Direct Lake", "a calculated table has no Lakehouse source" in cli_validate(rid)[1])
        plan(rid)

        def other_kpi(model):
            model.measures[0].kpi = "Customer Retention Rate"

        edit_model(rid, other_kpi)
        check("a measure of a KPI that isn't feasible fails", "measures[Net Revenue].kpi: Customer Retention Rate is not a feasible KPI" in cli_validate(rid)[1])
        plan(rid)

        def invented_role(model):
            model.roles.append(RlsRole(name="RLS1", description="made up"))

        edit_model(rid, invented_role)
        check("an RLS role the contract doesn't ask for fails", "PLAN.8: roles: 1 RLS role(s) but the contract's security section has 0" in cli_validate(rid)[1])
        plan(rid)

        # non-conformed source: the plan records it and validation fails
        t401.write_conformed(conformed_file, [C, D, H, DATE_TABLE])
        r = plan(rid)
        sources = {s["table"]: s for s in r.json()["execution_plan"]["sources"]}
        check("a source that is no longer conformed is recorded, fails PLAN.5, and bumps only the plan", sources[P]["conformed"] is False and r.json()["validation"]["passed"] is False
              and "sources[saleslt.SalesLT_Product]: not conformed and no exception" in checks_of(r.json())["PLAN.5"]["evidence"]["paths"]
              and store.get(rid).artifacts["ExecutionPlan"].version > store.get(rid).artifacts["ModelSpec"].version)
        t401.write_conformed(conformed_file, [P, C, D, H, DATE_TABLE])
        plan(rid)

        # AC3: security only from the contract
        rid2 = new_run(security=("Sales reps see only their region",))
        r = plan(rid2)
        roles = r.json()["model_spec"]["roles"]
        check("RLS roles come from the contract's security section, with no invented filter", [(x["name"], x["description"], x["applies_to"], x["table_filters"]) for x in roles]
              == [("RLS1", "Sales reps see only their region", ["Customer"], [])] and r.json()["validation"]["passed"] is True)

        # AC4: missing date table
        rid3 = new_run(kpis=(MON,), schemas=("saleslt",))
        r = plan(rid3)
        body = r.json()
        actions = body["execution_plan"]["data_engineering_actions"]
        check("no date table in scope: an action to create one, blocking and unowned", r.status_code == 200 and len(actions) == 1 and actions[0]["kind"] == "create_date_table"
              and actions[0]["blocking"] is True and actions[0]["owner"] is None and actions[0]["status"] == "open")
        check("no calculated or invented date table in the ModelSpec", all(t["role"] != "date" and t["source"] is not None for t in body["model_spec"]["tables"]))
        check("such a plan still validates", body["validation"]["passed"] is True)

        # bridges are carried into the plan
        t401.write_conformed(conformed_file, [P, C, D, H, DATE_TABLE, "nexora_test.dim_promotion", "nexora_test.bridge_product_promotion"])
        rid4 = store.create("Sales by promotion").run_id
        run = store.get(rid4)
        run.scope = RunScope(workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=["saleslt", "nexora_test"], sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"))
        run.state = RunState.REQUIREMENT_APPROVED
        store.save(run)
        (runs_root / rid4).mkdir(parents=True, exist_ok=True)
        bc.save(bc.BusinessContract(run_id=rid4, business_objective="Revenue", entities=["Sales", "Product", "Promotion"],
                                    kpis=[bc.Kpi(name=NR, grain="order line", currency="USD", **t402.KPIS[NR])]), runs_root / rid4 / "BusinessContract.yaml")
        for step in ("harvest", "profile", "relationships/analyze"):
            assert client.post(f"/runs/{rid4}/{step}", json={"candidates": [DETAIL_TO_HEADER]} if step.endswith("analyze") else None).status_code == 200
        run = store.get(rid4)
        run.state = RunState.DATA_PROFILED
        store.save(run)
        assert client.post(f"/runs/{rid4}/plan/sources").status_code == 200
        feasibility_llm.answers = [t402.default_answer()]
        assert client.post(f"/runs/{rid4}/plan/feasibility").status_code == 200
        assert client.post(f"/runs/{rid4}/plan/bridges").status_code == 200
        r = plan(rid4)
        body = r.json()
        bridge = next((t for t in body["model_spec"]["tables"] if t["role"] == "bridge"), None)
        check("a proposed bridge table joins the model as a bridge, and its id is in the plan", r.status_code == 200 and body["execution_plan"]["bridges"] == ["B1"] and bridge is not None
              and bridge["source"]["table"] == "bridge_product_promotion")
        check("the bridge relates to Product and Promotion", {(x["from"]["table"], x["to"]["table"]) for x in body["model_spec"]["relationships"] if x["from"]["table"] == bridge["name"]} == {(bridge["name"], "Product"), (bridge["name"], "Promotion")})

        # only feasible KPIs
        rid5 = new_run(kpis=(NR, RET))
        r = plan(rid5)
        check("only feasible KPIs become measures", r.status_code == 200 and [m["name"] for m in r.json()["model_spec"]["measures"]] == [NR])
        rid6 = new_run(kpis=(RET,))
        r = plan(rid6)
        check("every KPI a data gap: 409 NO_FEASIBLE_KPIS", envelope(r, 409, "NO_FEASIBLE_KPIS") and not (runs_root / rid6 / "ModelSpec.yaml").exists())

        # errors
        run = store.get(rid5)
        run.state = RunState.REQUIREMENT_APPROVED
        store.save(run)
        check("wrong state is 409 WRONG_STATE", envelope(plan(rid5), 409, "WRONG_STATE"))
        check("GET works in any state", client.get(f"/runs/{rid5}/plan").status_code == 200)
        check("before feasibility is 404 NOT_FOUND", envelope(plan(new_run(feasibility=False)), 404, "NOT_FOUND"))
        check("GET before a plan is 404", envelope(client.get(f"/runs/{new_run(feasibility=False)}/plan"), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/plan"), 404, "RUN_NOT_FOUND"))
        conformed_file.write_text("conformed: x\n", encoding="utf-8")
        check("an invalid conformed config is 500 CONFIG_ERROR", envelope(plan(rid), 500, "CONFIG_ERROR"))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
