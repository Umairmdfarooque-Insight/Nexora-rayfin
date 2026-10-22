"""T-402 offline checks: KPI feasibility, data gaps and the POST /runs/{id}/plan/feasibility endpoint.

Run: python -m app.manual_tests.t402_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse, SQL endpoint and model are fakes, and roles, conformed sources and the glossary
are temporary files (extending the fixtures of t302, t303 and t401), so no sign-in or network is
needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_feasibility_llm,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.discovery import glossary as glossary_module
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

# --- fixtures on top of t401: a date column ------------------------------------------------
base.LAKEHOUSE["saleslt"]["SalesLT_SalesOrderHeader"].append(col("OrderDate", "TIMESTAMP"))
base.DATA[("saleslt", "SalesLT_SalesOrderHeader")]["OrderDate"] = [base.D(2024, 1, 1)] * 20

LINE, STATUS, ODATE = f"{D}.LineTotal", f"{H}.Status", f"{H}.OrderDate"
NR, MON, RET = "Net Revenue", "Monthly Revenue", "Customer Retention Rate"
KPIS = {
    NR: dict(formula_intent="Sum of line totals excluding cancelled orders", time_logic=None),
    MON: dict(formula_intent="Sum of line totals", time_logic="by month"),
    RET: dict(formula_intent="Customers active in two consecutive quarters over those active in the first", time_logic="by quarter"),
}


def field(need, role, table, column):
    return {"need": need, "role": role, "candidates": [{"table": table, "column": column}]}


SOURCE_ANSWER = {
    "entities": [],
    "kpi_fields": [
        {"kpi": NR, "fields": [field("line amount", "measure", D, "LineTotal"), field("order status", "filter", H, "Status")]},
        {"kpi": MON, "fields": [field("line amount", "measure", D, "LineTotal"), field("order date", "time", H, "OrderDate")]},
        {"kpi": RET, "fields": []},
    ],
}


def need(text, role, mapped):
    return {"need": text, "role": role, "mapped_to": mapped}


def default_answer():
    return {
        "kpis": [
            {"kpi": NR, "needs": [need("line amount", "measure", LINE), need("order status", "filter", STATUS)]},
            {"kpi": MON, "needs": [need("line amount", "measure", LINE), need("order date", "time", ODATE)]},
            {"kpi": RET, "needs": [need("customer activity per period", "measure", None), need("quarter", "time", None)]},
        ]
    }


class FakeFeasibilityLlm:
    def __init__(self):
        self.answers: list = [default_answer()]
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, system, user, schema):
        self.calls.append((system, user, schema))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer if isinstance(answer, str) else json.dumps(answer)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = t303.EvidenceSql()
        roles_file, conformed_file, glossary_file = tmp_dir / "roles.yaml", tmp_dir / "conformed.yaml", tmp_dir / "glossary.yaml"
        t401.write_roles(roles_file)
        t401.write_conformed(conformed_file, [P, C, D, H])
        glossary_file.write_text(
            "terms:\n  product: [product, products]\n  customer: [customer, customers]\n  sales: [sales, salesorderdetail]\n",
            encoding="utf-8",
        )
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH = roles_file, conformed_file, glossary_file

        source_llm = t401.FakeSourceLlm(SOURCE_ANSWER)
        llm = FakeFeasibilityLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=UPN, name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: base.FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_source_llm] = lambda: source_llm
        app.dependency_overrides[get_feasibility_llm] = lambda: llm
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(NR,), resolve=True, state=RunState.DATA_PROFILED):
            run = store.create("Show net revenue by product")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=["saleslt"],
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"),
            )
            run.state = RunState.REQUIREMENT_APPROVED
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            contract = bc.BusinessContract(
                run_id=run.run_id,
                business_objective="Revenue",
                entities=["Sales", "Product"],
                kpis=[bc.Kpi(name=k, grain="order line", currency="USD", **KPIS[k]) for k in kpis],
            )
            bc.save(contract, run_dir / "BusinessContract.yaml")
            assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
            run = store.get(run.run_id)
            run.state = RunState.DATA_PROFILED
            store.save(run)
            if resolve:
                assert client.post(f"/runs/{run.run_id}/plan/sources").status_code == 200
            if state is not RunState.DATA_PROFILED:
                run = store.get(run.run_id)
                run.state = state
                store.save(run)
            return run.run_id

        def feasibility(rid):
            return client.post(f"/runs/{rid}/plan/feasibility")

        def by_kpi(body):
            return {k["kpi"]: k for k in body["kpis"]}

        def run_files_text(rid):
            return "\n".join(
                p.read_text(encoding="utf-8", errors="ignore").lower() for p in (runs_root / rid).rglob("*") if p.is_file()
            )

        # AC2: supported KPI passes
        rid = new_run((NR,))
        r = feasibility(rid)
        body = r.json()
        nr = by_kpi(body)[NR]
        check("supported KPI: 200 and feasible", r.status_code == 200 and nr["status"] == "feasible" and nr["missing"] == [] and nr["options"] == [])
        check("every needed field is mapped to a column", {n["need"]: n["mapped"] for n in nr["needed"]} == {"line amount": LINE, "order status": STATUS})
        check("a feasible run stays DATA_PROFILED, with no data gap", body["run"]["state"] == "DATA_PROFILED" and body["run"]["data_gaps"] == [])
        artifact = store.get(rid).artifacts["Feasibility"]
        check("Feasibility.json is saved and registered", (runs_root / rid / "Feasibility.json").is_file() and artifact.version == 1 and artifact.status == "draft"
              and "SourceResolution" in artifact.based_on)
        payload = json.loads(llm.calls[0][1])
        check("the model got KPI definitions and column names and types only", set(payload) == {"kpis", "available_columns"} and base.SECRET_NAME not in llm.calls[0][1]
              and all(set(c) == {"column", "type"} for c in payload["available_columns"]) and {c["column"] for c in payload["available_columns"]} == {LINE, STATUS})

        # deterministic: same inputs, same file, one model call
        first = (runs_root / rid / "Feasibility.json").read_bytes()
        llm.answers = [{"kpis": []}]
        r = feasibility(rid)
        check("a second call gives an identical file and version", r.status_code == 200 and (runs_root / rid / "Feasibility.json").read_bytes() == first
              and store.get(rid).artifacts["Feasibility"].version == 1 and len(llm.calls) == 1)
        llm.answers = [default_answer()]

        # AC3: deterministic override
        sr_path = runs_root / rid / "SourceResolution.json"
        resolution = json.loads(sr_path.read_text(encoding="utf-8"))
        for kpi in resolution["kpi_fields"]:
            kpi["fields"] = [f for f in kpi["fields"] if f["column"] != "LineTotal"]
        sr_path.write_text(json.dumps(resolution, indent=2), encoding="utf-8")
        r = feasibility(rid)
        nr = by_kpi(r.json())[NR]
        check("removing the LineTotal mapping makes Net Revenue a data gap, even though the model still claims it", r.status_code == 200 and nr["status"] == "data_gap"
              and [m["need"] for m in nr["missing"]] == ["line amount"] and "not a column mapped" in nr["missing"][0]["reason"])
        check("it is a new version, the run is NEEDS_INPUT and the gap is listed", store.get(rid).artifacts["Feasibility"].version == 2
              and r.json()["run"]["state"] == "NEEDS_INPUT" and r.json()["run"]["resume_state"] == "DATA_PROFILED"
              and len(r.json()["run"]["data_gaps"]) == 1 and "Net Revenue" in r.json()["run"]["data_gaps"][0]["description"])
        check("a run in NEEDS_INPUT can't be checked again (409)", envelope(feasibility(rid), 409, "WRONG_STATE"))

        # AC1: unsupported KPI stopped
        rid2 = new_run((NR, RET))
        r = feasibility(rid2)
        body = r.json()
        kpis = by_kpi(body)
        ret = kpis[RET]
        check("Customer Retention Rate is a data gap and Net Revenue is feasible", r.status_code == 200 and ret["status"] == "data_gap" and kpis[NR]["status"] == "feasible")
        check("the missing inputs are listed, with a reason", {m["need"] for m in ret["missing"]} == {"customer activity per period", "quarter"}
              and all(m["reason"] for m in ret["missing"]) and all(n["mapped"] is None for n in ret["needed"]))
        check("a data gap offers the three options", ret["options"] == ["drop_kpi", "provide_source", "data_engineering_task"])
        check("one data gap makes the run NEEDS_INPUT", body["run"]["state"] == "NEEDS_INPUT" and len(body["run"]["data_gaps"]) == 1
              and "Customer Retention Rate" in body["run"]["data_gaps"][0]["description"])
        check("the artifact is marked data_gap", store.get(rid2).artifacts["Feasibility"].status == "data_gap")
        text = run_files_text(rid2)
        check("no DAX or proxy measure anywhere in the run folder", "dax" not in text and "proxy" not in text and "evaluate" not in text)

        rid3 = new_run((RET,))
        r = feasibility(rid3)
        check("every KPI a data gap: NEEDS_INPUT", r.json()["run"]["state"] == "NEEDS_INPUT" and all(k["status"] == "data_gap" for k in r.json()["kpis"]))

        # partial support, hallucinated column, omitted need, time logic
        rid4 = new_run((NR,))
        llm.answers = [{"kpis": [{"kpi": NR, "needs": [need("line amount", "measure", LINE), need("order status", "filter", STATUS), need("cancellation flag", "filter", None)]}]}]
        r = feasibility(rid4)
        nr = by_kpi(r.json())[NR]
        check("one missing exclusion field makes the KPI a data gap, not partly feasible", nr["status"] == "data_gap" and [m["need"] for m in nr["missing"]] == ["cancellation flag"]
              and sum(1 for n in nr["needed"] if n["mapped"]) == 2)

        rid5 = new_run((NR,))
        llm.answers = [{"kpis": [{"kpi": NR, "needs": [need("line amount", "measure", f"{P}.NoSuchColumn")]}]}]
        nr = by_kpi(feasibility(rid5).json())[NR]
        check("a column the model names that the inventory lacks is a data gap", nr["status"] == "data_gap" and "NoSuchColumn" in nr["missing"][0]["reason"])

        rid7 = new_run((MON,))
        llm.answers = [default_answer()]
        r = feasibility(rid7)
        mon = by_kpi(r.json())[MON]
        check("time logic with a mapped date column is feasible", mon["status"] == "feasible" and {n["need"]: n["mapped"] for n in mon["needed"]}["order date"] == ODATE)

        rid8 = new_run((MON,))
        llm.answers = [{"kpis": [{"kpi": MON, "needs": [need("line amount", "measure", LINE)]}]}]
        mon = by_kpi(feasibility(rid8).json())[MON]
        check("time logic with no date need is a data gap naming the missing date", mon["status"] == "data_gap" and "date column for time logic: by month" in mon["missing"][0]["need"])

        rid9 = new_run((NR, MON))  # Status is then one of the mapped columns
        llm.answers = [{"kpis": [{"kpi": MON, "needs": [need("line amount", "measure", LINE), need("order date", "time", STATUS)]}]}]
        mon = by_kpi(feasibility(rid9).json())[MON]
        check("a time need mapped to a non-date column is a data gap", mon["status"] == "data_gap" and "not a date column" in mon["missing"][0]["reason"])

        rid10 = new_run((NR,))
        resolution = json.loads((runs_root / rid10 / "SourceResolution.json").read_text(encoding="utf-8"))
        resolution["kpi_fields"][0]["unmapped"] = [{"need": "currency rate", "role": "measure", "reason": "No column in the inventory matches this need"}]
        (runs_root / rid10 / "SourceResolution.json").write_text(json.dumps(resolution), encoding="utf-8")
        llm.answers = [default_answer()]
        nr = by_kpi(feasibility(rid10).json())[NR]
        check("a need the source resolution couldn't map counts even if the model omits it", nr["status"] == "data_gap" and [m["need"] for m in nr["missing"]] == ["currency rate"])

        # unusable model output
        rid11 = new_run((NR,))
        calls = len(llm.calls)
        llm.answers = ["not json", default_answer()]
        r = feasibility(rid11)
        check("one unusable answer is retried once, with the problem named", r.status_code == 200 and len(llm.calls) == calls + 2 and "unusable" in llm.calls[-1][1])
        rid12 = new_run((NR,))
        llm.answers = ["not json", '{"kpis": "x"}']
        r = feasibility(rid12)
        check("unusable twice is 502 LLM_INVALID_OUTPUT, state unchanged, nothing saved", envelope(r, 502, "LLM_INVALID_OUTPUT") and store.get(rid12).state is RunState.DATA_PROFILED
              and "Feasibility" not in store.get(rid12).artifacts and not (runs_root / rid12 / "Feasibility.json").exists()
              and not (runs_root / rid12 / "feasibility_suggestions.json").exists())
        llm.answers = [{"kpis": [{"kpi": NR, "needs": [{"need": "x", "role": "banana", "mapped_to": None}]}]}]
        check("an invalid role is unusable output too", envelope(feasibility(rid12), 502, "LLM_INVALID_OUTPUT"))
        llm.answers = [default_answer()]

        # errors
        check("before sources are resolved is 404 NOT_FOUND", envelope(feasibility(new_run(resolve=False)), 404, "NOT_FOUND"))
        run_a = new_run()
        run = store.get(run_a)
        run.state = RunState.REQUIREMENT_APPROVED
        store.save(run)
        check("wrong state is 409 WRONG_STATE", envelope(feasibility(run_a), 409, "WRONG_STATE"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/plan/feasibility"), 404, "RUN_NOT_FOUND"))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
