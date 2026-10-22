"""T-504 offline checks: the G4 checks, the Power BI client, the reference SQL guard, the
ValidationReport and `POST /runs/{id}/model/validate`.

Run: python -m app.manual_tests.t504_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI, the SQL endpoint and the model are fakes, and the run is written by hand into a
temporary folder, so no sign-in or network is needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
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
from app.compiler.critical_terms import ConfigError
from app.connectors.fabric_api import FabricTimeout
from app.connectors.powerbi_api import DaxError, PowerBiClient, RefreshFailed
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, RlsRole, load_yaml, save_yaml
from app.deploy.fabric_items import FabricOperationFailed
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.model import validation_config
from app.model.recon_sql import check_sql
from app.model.tmdl_reader import parse_role, read_model
from app.model.validation_config import load_tolerance
from app.store.file_store import FileRunStore
from app.workflow.run import RunScope
from app.workflow.states import RunState, can_transition

check = t502.check
NR, MON = t503.NR, t503.MON
SANDBOX = "33333333-3333-4333-8333-333333333333"

TOTAL_SQL = (
    "SELECT SUM(d.[LineTotal]) AS value FROM [saleslt].[SalesLT_SalesOrderDetail] d "
    "JOIN [saleslt].[SalesLT_SalesOrderHeader] h ON h.[SalesOrderID] = d.[SalesOrderID] WHERE h.[Status] <> 6"
)
SLICE_SQL = (
    "SELECT p.[Name] AS label, SUM(d.[LineTotal]) AS value FROM [saleslt].[SalesLT_SalesOrderDetail] d "
    "JOIN [saleslt].[SalesLT_SalesOrderHeader] h ON h.[SalesOrderID] = d.[SalesOrderID] "
    "JOIN [saleslt].[SalesLT_Product] p ON p.[ProductID] = d.[ProductID] WHERE h.[Status] <> 6 GROUP BY p.[Name]"
)
RECON_ANSWER = json.dumps(
    {"total_sql": TOTAL_SQL, "slice_table": "Product", "slice_column": "Product Name", "slice_sql": SLICE_SQL, "explanation": "ok"}
)


class FakeItems:
    def __init__(self):
        self.calls, self.error = [], None

    def upsert_semantic_model(self, workspace_id, display_name, tmdl_dir):
        self.calls.append((workspace_id, display_name, Path(tmdl_dir)))
        if self.error:
            raise self.error
        return {"item_id": "ds-1", "operation": "created"}


class FakePbi:
    """Answers executeQueries by the measure in the query, and counts rows for RLS queries."""

    def __init__(self):
        self.values = {NR: 1000.0, MON: 1000.0}
        self.slice_value = 400.0
        self.errors: dict[str, str] = {}
        self.refresh_error = None
        self.refreshed, self.queries = [], []
        self.rls_rows = (100.0, 40.0)  # (without identity, with identity)

    def refresh(self, workspace_id, dataset_id, timeout_s=600):
        self.refreshed.append((workspace_id, dataset_id))
        if self.refresh_error:
            raise self.refresh_error

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        self.queries.append((dax, impersonate))
        if "COUNTROWS" in dax:
            return [{"[n]": self.rls_rows[1] if impersonate else self.rls_rows[0]}]
        for name, message in self.errors.items():
            if f"[{name}]" in dax:
                raise DaxError(message)
        for name, value in self.values.items():
            if f"[{name}]" in dax:
                return [{"[v]": self.slice_value if "CALCULATE" in dax else value}]
        raise DaxError("unknown measure")


class FakeCursor:
    def __init__(self, sql):
        self.sql, self.last = sql, []

    def execute(self, statement):
        self.sql.queries.append(statement)
        self.last = self.sql.answer(statement)

    def fetchone(self):
        return tuple(self.last[0]) if self.last else None

    def fetchall(self):
        return [tuple(r) for r in self.last]

    def fetchmany(self, n):
        return [tuple(r) for r in self.last[:n]]


class FakeSql:
    def __init__(self):
        self.queries: list[str] = []
        self.total = 1000.0
        self.mon_total = 1000.0
        self.dim_dupes = 0
        self.orphans = 0
        self.slice_rows = [("Alpha", 400.0), ("Beta", 600.0)]
        self.fail_on: str | None = None

    def answer(self, statement: str):
        if self.fail_on and self.fail_on in statement:
            raise Exception("42S02", "Invalid object name")
        if statement.startswith("SELECT COUNT_BIG(*), COUNT_BIG(DISTINCT"):
            return [(10, 10 - self.dim_dupes if "[Product]" in statement or "dim" in statement else 10)]
        if "LEFT JOIN (SELECT DISTINCT" in statement:
            return [(50, self.orphans)]
        if "GROUP BY" in statement:
            return self.slice_rows
        return [(self.total,)] if "<> 6" in statement else [(self.mon_total,)]

    def connect(self):
        outer = self

        class Connection:
            timeout = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor(outer)

        return Connection()


MON_RECON_ANSWER = json.dumps(
    {
        "total_sql": "SELECT SUM(d.[LineTotal]) AS value FROM [saleslt].[SalesLT_SalesOrderDetail] d",
        "slice_table": None, "slice_column": None, "slice_sql": None, "explanation": "ok",
    }
)


class ReconLlm:
    """Answers per KPI (a dict by KPI name), or with a list of answers in order."""

    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    def __call__(self, system, user, schema):
        self.calls += 1
        if isinstance(self.answers, dict):
            return self.answers[json.loads(user.split("\n\n")[0])["kpi"]["name"]]
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX

    # --- config and the state machine ---------------------------------------------------------
    tol = load_tolerance()
    check("tolerance comes from config/validation.yaml", (tol.relative, tol.absolute) == (0.0001, 0.01))
    check("tolerance: within and outside", tol.allows(1000.005, 1000) and tol.allows(1000.09, 1000) and not tol.allows(1000.2, 1000))
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "validation.yaml"
        bad.write_text("reconciliation:\n  relative_tolerance: -1\n", encoding="utf-8")
        try:
            load_tolerance(bad)
            ok = False
        except ConfigError as exc:
            ok = "relative_tolerance" in str(exc)
        check("an invalid validation.yaml is a ConfigError naming the key", ok)
    check("a failed model can only go back to generation, never to the report stage",
          not can_transition(RunState.MODEL_FAILED, RunState.REPORT_GENERATING) and can_transition(RunState.MODEL_FAILED, RunState.MODEL_GENERATING))

    # --- the reference SQL guard ---------------------------------------------------------------
    inventory = t502.build_inventory(t503.spec_with_measures())
    kpi = t503.KPI_NR
    check("sql: the reference query passes", check_sql(TOTAL_SQL, inventory, kpi, "q") == [])
    check("sql: INSERT is refused", any("INSERT" in e for e in check_sql("INSERT INTO [saleslt].[SalesLT_Product] SELECT 1", inventory, kpi, "q")))
    check("sql: two statements are refused", any("single statement" in e for e in check_sql("SELECT 1; DROP TABLE x", inventory, kpi, "q")))
    check("sql: a table outside the inventory is refused", any("[schema].[table]" in e for e in check_sql("SELECT COUNT(*) FROM [saleslt].[Nope]", inventory, kpi, "q")))
    check("sql: an unstated literal is refused", any("hardcodes 0.8" in e for e in check_sql("SELECT SUM(d.[LineTotal]) * 0.8 FROM [saleslt].[SalesLT_SalesOrderDetail] d", inventory, kpi, "q")))
    check("sql: a CTE is not mistaken for a table", check_sql("WITH x AS (SELECT [LineTotal] FROM [saleslt].[SalesLT_SalesOrderDetail]) SELECT SUM([LineTotal]) FROM x", inventory, kpi, "q") == [])

    # --- the Power BI client -------------------------------------------------------------------
    class Resp:
        def __init__(self, status=200, body=None, headers=None):
            self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}

        def json(self):
            return self._body

    sent, queue = [], []

    def http(method, url, headers=None, json=None, timeout=None):
        sent.append((method, url, json))
        return queue.pop(0)

    clock = {"t": 0.0}
    client = PowerBiClient(token_provider=lambda: "t", http=http, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    queue[:] = [Resp(202, {}, {"x-ms-request-id": "req-1"}), Resp(200, {"status": "Unknown"}), Resp(200, {"status": "Completed"})]
    client.refresh("ws", "ds")
    check("refresh: POSTs, then polls the request until Completed",
          sent[0][:2] == ("POST", "https://api.powerbi.com/v1.0/myorg/groups/ws/datasets/ds/refreshes") and sent[1][1].endswith("/refreshes/req-1") and len(sent) == 3)
    queue[:] = [Resp(202, {}, {"x-ms-request-id": "req-1"}), Resp(200, {"status": "Failed", "serviceExceptionJson": "Direct Lake: table missing"})]
    try:
        client.refresh("ws", "ds")
        raised = None
    except RefreshFailed as exc:
        raised = exc
    check("refresh: a failed refresh carries Power BI's text", raised is not None and "table missing" in str(raised))
    queue[:] = [Resp(202, {}, {"x-ms-request-id": "req-1"})] + [Resp(200, {"status": "Unknown"})] * 400
    try:
        client.refresh("ws", "ds", timeout_s=30)
        raised = None
    except FabricTimeout as exc:
        raised = exc
    check("refresh: a refresh that doesn't finish is a timeout", raised is not None)
    sent.clear()
    queue[:] = [Resp(200, {"results": [{"tables": [{"rows": [{"[v]": 5}]}]}]})]
    rows = client.execute_dax("ws", "ds", "EVALUATE ROW(\"v\", [M])", impersonate="a@b.com")
    check("executeQueries: body has the query, includeNulls and the impersonated user", rows == [{"[v]": 5}]
          and sent[0][2] == {"queries": [{"query": 'EVALUATE ROW("v", [M])'}], "serializerSettings": {"includeNulls": True}, "impersonatedUserName": "a@b.com"})
    queue[:] = [Resp(400, {"error": {"code": "DatasetExecuteQueriesError", "pbi.error": {"details": [{"code": "DetailsMessage", "detail": {"type": 1, "value": "Column 'LineTotalX' not found"}}]}}})]
    try:
        client.execute_dax("ws", "ds", "x")
        raised = None
    except DaxError as exc:
        raised = exc
    check("executeQueries: the engine's message is the error", raised is not None and "LineTotalX" in str(raised))
    queue[:] = [Resp(429, {}, {"Retry-After": "2"}), Resp(429, {}, {"Retry-After": "2"}), Resp(200, {"results": [{"tables": [{"rows": []}]}]})]
    check("executeQueries: a 429 is retried", client.execute_dax("ws", "ds", "x") == [])
    queue[:] = [Resp(429, {}, {"Retry-After": "1"})] * 10
    try:
        client.execute_dax("ws", "ds", "x")
        raised = None
    except Exception as exc:
        raised = exc
    check("executeQueries: throttling that doesn't end is an upstream error", raised is not None and "throttling" in str(raised))

    # --- the TMDL reader reads roles ---------------------------------------------------------------
    role = parse_role("role RLS1\n\tmodelPermission: read\n\n\ttablePermission Sales = 'Sales Order'[Status] <> 6\n")
    check("tmdl reader: role and table filter", role.name == "RLS1" and role.filters == {"Sales": "'Sales Order'[Status] <> 6"})

    # --- the endpoint --------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        items, pbi, sql, recon = FakeItems(), FakePbi(), FakeSql(), ReconLlm({NR: RECON_ANSWER, MON: MON_RECON_ANSWER})
        dax_llm = t503.FakeLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        app.dependency_overrides[get_dax_llm] = lambda: dax_llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_recon_llm] = lambda: recon
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(t503.KPI_NR, t503.KPI_MON), measures=True, state=RunState.PLAN_APPROVED):
            run = store.create("Show net revenue by product")
            spec = t503.spec_with_measures(run_id=run.run_id)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            t502_plan = t502.build_plan(spec)
            save_yaml(spec, run_dir / "ModelSpec.yaml")
            save_yaml(t502_plan, run_dir / "ExecutionPlan.yaml")
            from app.workflow.harvest_stage import canonical_hash, write_json
            from app.contracts.model_plan import to_data
            from app.workflow.run import ArtifactRef, utc_now

            write_json(run_dir / "MetadataInventory.json", t502.build_inventory(spec).to_dict())
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Revenue", entities=["Sales"], kpis=list(kpis)), run_dir / "BusinessContract.yaml")
            run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
            for name, model in (("ModelSpec", spec), ("ExecutionPlan", t502_plan)):
                run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.state = state
            store.save(run)
            if state is RunState.PLAN_APPROVED:
                assert client.post(f"/runs/{run.run_id}/model/generate").status_code == 200
                if measures:
                    dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
                    assert client.post(f"/runs/{run.run_id}/model/measures").status_code == 200
            return run.run_id

        def validate(run_id):
            return client.post(f"/runs/{run_id}/model/validate")

        def checks_of(r):
            return {(c["id"], c["object"]): c for c in r.json()["report"]["checks"]}

        # 1. A passing model
        run_id = new_run()
        r = validate(run_id)
        body = r.json()
        if r.status_code != 200 or not body["report"]["passed"]:
            print([(c["id"], c["object"], c["status"], c["message"]) for c in body["report"]["checks"] if c["status"] != "pass"] if "report" in body else body)
        by = checks_of(r)
        check("validate: 200, passed, state stays MODEL_GENERATING", r.status_code == 200 and body["report"]["passed"] is True and body["run"]["state"] == "MODEL_GENERATING"
              and not [c for c in body["report"]["checks"] if c["status"] == "fail"])
        check("the current TMDL version was deployed to the sandbox workspace", items.calls[-1][0] == SANDBOX and items.calls[-1][2].name == "v2"
              and items.calls[-1][1] == f"Nexora_{run_id[:8]}" and pbi.refreshed[-1] == (SANDBOX, "ds-1"))
        check("report entry: artifact, version, attempt", body["report"]["artifact"] == "SemanticModel" and body["report"]["artifact_version"] == 2 and body["report"]["attempt"] == 1)
        recon_check = by[("G4.RECON", f"measure:{NR}")]
        check("G4.RECON evidence shows the DAX and SQL values side by side",
              recon_check["status"] == "pass" and recon_check["evidence"]["dax_value"] == 1000.0 and recon_check["evidence"]["sql_value"] == 1000.0
              and recon_check["evidence"]["sql_query"] == TOTAL_SQL and "no business example provided" in recon_check["evidence"]["notes"]
              and recon_check["evidence"]["slice"]["label"] == "Alpha" and recon_check["evidence"]["slice"]["dax_value"] == 400.0)
        check("the DAX is executed per measure", ('EVALUATE ROW("v", [Net Revenue])', None) in pbi.queries)
        check("G4.REF, G4.REL, G4.DAX, G4.RLS all passed", by[("G4.REF", "model")]["status"] == "pass" and by[("G4.DAX", f"measure:{NR}")]["status"] == "pass"
              and by[("G4.RLS", "model")]["message"] == "no roles defined" and any(k[0] == "G4.REL" and v["status"] == "pass" for k, v in by.items()))
        report = json.loads((runs_root / run_id / "ValidationReport.json").read_text(encoding="utf-8"))
        check("ValidationReport.json holds the run, each check with a fingerprint", len(report["runs"]) == 1 and report["run_id"] == run_id
              and all(c["fingerprint"].startswith("sha256:") for c in report["runs"][0]["checks"]) and all(c["classification"] is None for c in report["runs"][0]["checks"]))
        artifact = store.get(run_id).artifacts["ValidationReport"]
        check("the report is registered as an artifact", artifact.status == "passed" and artifact.version == 1)

        # a second validation appends and replays the saved reference SQL
        calls = recon.calls
        r = validate(run_id)
        report = json.loads((runs_root / run_id / "ValidationReport.json").read_text(encoding="utf-8"))
        check("a second validation appends attempt 2 and makes no new model call", r.json()["report"]["attempt"] == 2 and len(report["runs"]) == 2 and recon.calls == calls)

        # 2. Value differs beyond the tolerance
        sql.total = 900.0
        run_id = new_run()
        r = validate(run_id)
        c = checks_of(r)[("G4.RECON", f"measure:{NR}")]
        check("a differing value fails G4.RECON with both values and the difference", c["status"] == "fail" and c["code"] == "RECON_MISMATCH"
              and c["evidence"]["dax_value"] == 1000.0 and c["evidence"]["sql_value"] == 900.0 and c["evidence"]["difference"] == 100.0 and "differs from the SQL value" in c["message"])
        check("a failed check moves the run to MODEL_FAILED", r.json()["run"]["state"] == "MODEL_FAILED" and store.get(run_id).state is RunState.MODEL_FAILED
              and store.get(run_id).artifacts["ValidationReport"].status == "failed")
        sql.total = 1000.0
        sql.total = 1000.005
        run_id = new_run()
        check("a value inside the tolerance passes", validate(run_id).json()["report"]["passed"] is True)
        sql.total = 1000.0

        # 3. A wrong business example
        wrong = t503.KPI_NR.model_copy(update={"expected_examples": [bc.ExpectedExample(description="Q1 net revenue", value=123.0)]})
        run_id = new_run(kpis=(wrong, t503.KPI_MON))
        r = validate(run_id)
        c = checks_of(r)[("G4.RECON", f"measure:{NR}")]
        check("a wrong expected_example fails G4.RECON showing both values", c["status"] == "fail" and c["evidence"]["expected_example"]["value"] == 123.0
              and c["evidence"]["dax_value"] == 1000.0 and "business example 123.0" in c["message"])
        right = t503.KPI_NR.model_copy(update={"expected_examples": [bc.ExpectedExample(description="total", value=1000)]})
        check("a matching expected_example passes", validate(new_run(kpis=(right, t503.KPI_MON))).json()["report"]["passed"] is True)

        # 4. A broken measure in the deployed files
        run_id = new_run()
        sales = runs_root / run_id / "model" / "v2" / "definition" / "tables" / "Sales.tmdl"
        sales.write_bytes(sales.read_bytes().replace(b"Sales[LineTotal]", b"Sales[LineTotalX]"))
        pbi.errors = {NR: "Column 'LineTotalX' not found in table 'Sales'"}
        r = validate(run_id)
        by = checks_of(r)
        ref = by[("G4.REF", f"measure:{NR}")]
        dax = by[("G4.DAX", f"measure:{NR}")]
        check("a broken measure fails G4.REF and G4.DAX with the error text", ref["status"] == "fail" and "LineTotalX" in ref["message"] and ref["code"] == "UNKNOWN_REFERENCE"
              and dax["status"] == "fail" and "LineTotalX" in dax["message"] and dax["code"] == "DAX_ERROR" and r.json()["run"]["state"] == "MODEL_FAILED")
        check("files that differ from the generated version are noted", by[("G4.REF", "model")]["code"] == "FILES_CHANGED")
        check("the failure is classified later, not now", dax["classification"] is None)
        pbi.errors = {}

        # 5. Refresh or deployment failing
        run_id = new_run()
        pbi.refresh_error = RefreshFailed("The refresh failed: Direct Lake framing error")
        r = validate(run_id)
        by = checks_of(r)
        check("a failed refresh fails G4.DAX with its error and skips the rest",
              by[("G4.DAX", "model")]["code"] == "REFRESH_FAILED" and "framing error" in by[("G4.DAX", "model")]["message"]
              and all(by[(i, "model")]["status"] == "skipped" for i in ("G4.REL", "G4.RECON", "G4.RLS")) and r.json()["run"]["state"] == "MODEL_FAILED")
        pbi.refresh_error = None
        run_id = new_run()
        items.error = FabricOperationFailed("Invalid TMDL", "req-7", {"errorCode": "InvalidDefinition"})
        by = checks_of(validate(run_id))
        check("a deployment Fabric rejects fails G4.DAX with DEPLOY_FAILED", by[("G4.DAX", "model")]["code"] == "DEPLOY_FAILED" and "Invalid TMDL" in by[("G4.DAX", "model")]["message"])
        items.error = None

        # 6. An invalid relationship (duplicate keys on the one side)
        sql.dim_dupes = 3
        run_id = new_run()
        r = validate(run_id)
        rel = [c for c in r.json()["report"]["checks"] if c["id"] == "G4.REL" and c["status"] == "fail"]
        check("duplicate keys on the one side fail G4.REL with the evidence", rel and rel[0]["code"] == "RELATIONSHIP_INVALID" and "3 duplicate key" in rel[0]["message"]
              and rel[0]["evidence"]["to_duplicate_keys"] == 3 and rel[0]["evidence"]["queries"] and r.json()["run"]["state"] == "MODEL_FAILED")
        sql.dim_dupes = 0
        sql.orphans = 25
        r = validate(new_run())
        rel = [c for c in r.json()["report"]["checks"] if c["id"] == "G4.REL" and c["status"] == "fail"]
        check("too many orphans fail G4.REL", rel and "orphan rate 50.0%" in rel[0]["message"])
        sql.orphans = 1
        r = validate(new_run())
        check("a few orphans are a warning, not a failure", r.json()["report"]["passed"] is True and any(c["id"] == "G4.REL" and c["status"] == "warning" for c in r.json()["report"]["checks"]))
        sql.orphans = 0

        # 7. Blank measures
        pbi.values[MON] = None
        sql.mon_total = None
        r = validate(new_run())
        c = checks_of(r)[("G4.DAX", f"measure:{MON}")]
        check("a blank measure with no expected value is a warning", c["status"] == "warning" and c["code"] == "BLANK_RESULT" and r.json()["report"]["passed"] is True)
        expecting = t503.KPI_MON.model_copy(update={"expected_examples": [bc.ExpectedExample(description="m", value=10)]})
        r = validate(new_run(kpis=(t503.KPI_NR, expecting)))
        check("a blank measure with an expected value fails", checks_of(r)[("G4.DAX", f"measure:{MON}")]["status"] == "fail" and r.json()["run"]["state"] == "MODEL_FAILED")
        pbi.values[MON] = 1000.0
        sql.mon_total = 1000.0

        # 8. No usable reference SQL
        bad_recon = ReconLlm([json.dumps({"total_sql": "DROP TABLE x", "slice_table": None, "slice_column": None, "slice_sql": None, "explanation": ""})])
        app.dependency_overrides[get_recon_llm] = lambda: bad_recon
        r = validate(new_run())
        c = checks_of(r)[("G4.RECON", f"measure:{NR}")]
        check("no usable reference SQL (2 attempts) fails G4.RECON", c["status"] == "fail" and c["code"] == "RECON_REFERENCE_UNAVAILABLE" and bad_recon.calls >= 2)
        r = validate(new_run(kpis=(right, t503.KPI_MON.model_copy(update={"expected_examples": [bc.ExpectedExample(description="m", value=1000)]}))))
        c = checks_of(r)[("G4.RECON", f"measure:{NR}")]
        check("with a matching business example the missing SQL reference is only a warning", c["status"] == "warning" and r.json()["report"]["passed"] is True)
        app.dependency_overrides[get_recon_llm] = lambda: recon

        # 9. RLS
        def with_role(filters: str | None):
            rid = new_run()
            spec_path = runs_root / rid / "ModelSpec.yaml"
            spec = load_yaml(ModelSpec, spec_path)
            spec.roles = [RlsRole(name="RLS1", description="Reps see their region")]
            save_yaml(spec, spec_path)
            if filters is not None:
                folder = runs_root / rid / "model" / "v2" / "definition" / "roles"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "RLS1.tmdl").write_bytes(f"role RLS1\n\tmodelPermission: read\n\n\ttablePermission Sales = {filters}\n".encode("utf-8"))
            return rid

        c = checks_of(validate(with_role(None)))[("G4.RLS", "role:RLS1")]
        check("a role the model doesn't define fails G4.RLS", c["status"] == "fail" and c["code"] == "RLS_ROLE_MISSING")
        c = checks_of(validate(with_role("'Sales Order'[Nope] = 1")))[("G4.RLS", "role:RLS1")]
        check("a filter that references a missing column fails G4.RLS", c["status"] == "fail" and c["code"] == "RLS_FILTER_INVALID" and "Nope" in c["message"])
        r = validate(with_role("'Sales Order'[Status] <> 6"))
        c = checks_of(r)[("G4.RLS", "role:RLS1")]
        check("a valid filter that returns fewer rows as the identity passes", c["status"] == "pass"
              and c["evidence"]["Sales"] == {"rows_without_identity": 100.0, "rows_with_identity": 40.0}
              and any(imp == "t@contoso.com" for _, imp in pbi.queries))
        pbi.rls_rows = (100.0, 100.0)
        c = checks_of(validate(with_role("'Sales Order'[Status] <> 6")))[("G4.RLS", "role:RLS1")]
        check("a filter that returns all rows is a warning", c["status"] == "warning" and c["code"] == "RLS_NOT_FILTERING")
        pbi.rls_rows = (100.0, 40.0)

        # 10. Errors outside the checks
        check("wrong state: 409", envelope(validate(new_run(state=RunState.DATA_PROFILED)), 409, "WRONG_STATE"))
        no_model = new_run(state=RunState.MODEL_GENERATING)
        check("before the model is generated: 404", envelope(validate(no_model), 404, "NOT_FOUND"))
        del os.environ["SANDBOX_WORKSPACE_ID"]
        os.environ.pop("FABRIC_SANDBOX_WORKSPACE_ID", None)
        check("no sandbox workspace configured: 500 CONFIG_ERROR", envelope(validate(new_run()), 500, "CONFIG_ERROR"))
        os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
        check("unknown run: 404", envelope(validate("nope"), 404, "RUN_NOT_FOUND"))
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
