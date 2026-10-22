"""T-503 offline checks: DAX proposals, the reference pre-check, the `dax_measure` graph and
`POST /runs/{id}/model/measures`.

Run: python -m app.manual_tests.t503_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The model is a fake that returns scripted answers, and the run, inventory and approved plan are
written by hand into a temporary folder, so no sign-in or network is needed. A real model, the
sandbox deployment and the comparison with SQL are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_dax_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import Measure, ModelSpec, save_yaml, to_data, load_yaml
from app.graphs.base import run_graph
from app.graphs.dax_measure import build_dax_measure_graph
from app.llm.errors import LlmTimeout
from app.manual_tests import t502_offline_checks as t502
from app.model.dax_generator import (
    DaxRejected,
    approved_description,
    extract_references,
    propose_measure_dax,
)
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
NR, MON = "Net Revenue", "Monthly Revenue"
NR_DAX = "CALCULATE(SUM(Sales[LineTotal]), 'Sales Order'[Status] <> 6)"
MON_DAX = "CALCULATE([Net Revenue], ALL('Date'), VALUES('Date'[Month]))"


def answer(name, dax, refs, explanation="ok"):
    return json.dumps({"measure_name": name, "dax": dax, "referenced_objects": refs, "explanation": explanation})


def col(table, column):
    return {"table": table, "column": column, "measure": None}


def meas(name):
    return {"table": None, "column": None, "measure": name}


NR_ANSWER = answer(NR, NR_DAX, [col("Sales", "LineTotal"), col("Sales Order", "Status")])
MON_ANSWER = answer(MON, MON_DAX, [meas(NR), col("Date", "Month")])

KPI_NR = bc.Kpi(
    name=NR, formula_intent="Sum of line totals excluding cancelled orders", grain="order line", currency="USD",
    exclusions=["Cancelled orders have Status = 6"],
)
KPI_MON = bc.Kpi(name=MON, formula_intent="Net revenue by month", grain="month", currency="USD", time_logic="by month")


class FakeLlm:
    """Answers by the measure named in the request: a list of raw answers, used in order."""

    def __init__(self):
        self.answers: dict[str, list] = {}
        self.calls: list[tuple[str, str]] = []  # (measure, user)

    def __call__(self, system, user, schema):
        name = json.loads(user.split("\n\n")[0])["measure_name"]
        self.calls.append((name, user))
        queue = self.answers[name]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


def spec_with_measures(run_id="run-1", date_table=True):
    spec = t502.build_spec(run_id=run_id)
    spec.tables[1].columns.append(t502.col("Status", "int64"))
    spec.measures = [
        Measure(name=NR, table="Sales", kpi=NR, formula_intent=KPI_NR.formula_intent, description=f"Approved: {KPI_NR.formula_intent}"),
        Measure(name=MON, table="Sales", kpi=MON, formula_intent=KPI_MON.formula_intent),
    ]
    if not date_table:
        spec.tables = [t for t in spec.tables if t.role != "date"]
        spec.relationships = [r for r in spec.relationships if "Date" not in (r.from_.table, r.to.table)]
    return spec


def main() -> None:
    # --- reading DAX -----------------------------------------------------------------------------
    columns, bare, rest = extract_references(
        "VAR total = SUM('Sales Order'[Order ID]) // Sales[Ignored]\n"
        "RETURN DIVIDE(total, CALCULATE(COUNTROWS(Sales), Sales[Qty] > 1), 0) + [Other] + \"Product[Fake]\" + /* Sales[Gone] */ 0"
    )
    check("extract: quoted and plain tables, bare measures; comments and strings ignored",
          columns == [("Sales Order", "Order ID"), ("Sales", "Qty")] and bare == ["Other"])
    columns, bare, _ = extract_references("SUM(Sales[Odd]]Name]) + RETURN [M]".replace("RETURN [M]", "0"))
    check("extract: ]] inside a column name", columns == [("Sales", "Odd]Name")])
    check("extract: RETURN [Measure] is a measure, not a table", extract_references("VAR x = 1 RETURN [Total]")[:2] == ([], ["Total"]))

    # --- the pre-check through propose_measure_dax -------------------------------------------------
    spec = spec_with_measures()
    llm = FakeLlm()

    def propose(kpi, measure, *answers, spec=spec):
        llm.answers[measure] = list(answers)
        before = llm.count(measure)
        try:
            return propose_measure_dax(kpi, spec, llm, measure), llm.count(measure) - before
        except DaxRejected as exc:
            return exc, llm.count(measure) - before

    ok, calls = propose(KPI_NR, NR, NR_ANSWER)
    check("accepts a valid proposal in one call", ok.dax == NR_DAX and calls == 1)
    check("the model gets names and types only", all(k in json.loads(llm.calls[0][1]) for k in ("kpi", "tables", "other_measures", "relationships"))
          and "rows" not in llm.calls[0][1])

    bad = answer(NR, "SUM(Sales[Nope])", [col("Sales", "Nope")])
    r, calls = propose(KPI_NR, NR, bad, NR_ANSWER)
    check("unknown column: one retry that carries the error, then accepted", r.dax == NR_DAX and calls == 2
          and "unknown column 'Nope'" in llm.calls[-1][1])
    r, calls = propose(KPI_NR, NR, bad)
    check("unknown column twice: rejected after exactly 2 attempts, naming the reference",
          isinstance(r, DaxRejected) and calls == 2 and "Sales[Nope]" in r.reason and len(r.attempts) == 2)

    mismatch = answer(NR, NR_DAX, [col("Sales", "LineTotal")])
    r, _ = propose(KPI_NR, NR, mismatch)
    check("referenced_objects that disagrees with the DAX text is rejected", isinstance(r, DaxRejected) and "referenced_objects doesn't match" in r.reason and "status" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "SUM([LineTotal])", [col("Sales", "LineTotal")]))
    check("an unqualified column is rejected", isinstance(r, DaxRejected) and "unqualified column [LineTotal]" in r.reason and "Sales" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "SUM(Nowhere[LineTotal])", [col("Nowhere", "LineTotal")]))
    check("an unknown table is rejected", isinstance(r, DaxRejected) and "unknown table 'Nowhere'" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "[Ghost] + 0", [meas("Ghost")]))
    check("an unknown measure is rejected", isinstance(r, DaxRejected) and "[Ghost]" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "[Net Revenue] + 0", [meas(NR)]))
    check("a measure that refers to itself is rejected", isinstance(r, DaxRejected) and "refers to itself" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "SUM(Sales[LineTotal]) / COUNTROWS(Sales)", [col("Sales", "LineTotal")]))
    check("the / operator is rejected in favour of DIVIDE", isinstance(r, DaxRejected) and "DIVIDE" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "DIVIDE(SUM(Sales[LineTotal]), COUNTROWS(Sales), 0)", [col("Sales", "LineTotal")]))
    check("DIVIDE with 0 as the alternate result is accepted", not isinstance(r, DaxRejected))
    r, _ = propose(KPI_NR, NR, answer(NR, "SUM(Sales[LineTotal]) * 0.8", [col("Sales", "LineTotal")]))
    check("a literal the KPI doesn't state is rejected", isinstance(r, DaxRejected) and "hardcoded literal 0.8" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, 'IF(SUM(Sales[LineTotal]) > 0, "Positive", BLANK())', [col("Sales", "LineTotal")]))
    check("a text literal the KPI doesn't state is rejected", isinstance(r, DaxRejected) and "hardcoded text 'Positive'" in r.reason)
    r, _ = propose(KPI_NR, NR, answer("Other", NR_DAX, [col("Sales", "LineTotal"), col("Sales Order", "Status")]))
    check("a proposal for another measure name is rejected", isinstance(r, DaxRejected) and "measure_name" in r.reason)
    r, calls = propose(KPI_NR, NR, "not json", "still not json")
    check("unusable output twice is rejected", isinstance(r, DaxRejected) and calls == 2 and "JSON" in r.reason)
    r, _ = propose(KPI_NR, NR, answer(NR, "SUM(sales[linetotal])", [col("SALES", "LINETOTAL")]))
    check("names match ignoring case, as in DAX", not isinstance(r, DaxRejected))
    r, _ = propose(KPI_NR, NR, answer(NR, "-- total\nSUM(Sales[LineTotal])", [col("Sales", "LineTotal")]))
    check("comments in the DAX are ignored by the pre-check", not isinstance(r, DaxRejected))

    # --- date table -------------------------------------------------------------------------------
    no_date = spec_with_measures(date_table=False)
    r, calls = propose(KPI_MON, MON, MON_ANSWER, spec=no_date)
    check("time logic without a date table: 'date table required', and the model isn't asked", isinstance(r, DaxRejected) and "date table required" in r.reason and calls == 0)
    r, _ = propose(KPI_NR, NR, answer(NR, "CALCULATE(SUM(Sales[LineTotal]), SAMEPERIODLASTYEAR(Sales[LineTotal]))", [col("Sales", "LineTotal")]), spec=no_date)
    check("time intelligence in the DAX without a date table is rejected", isinstance(r, DaxRejected) and "date table required" in r.reason)
    r, _ = propose(KPI_MON, MON, MON_ANSWER)
    check("time logic with a date table is accepted", not isinstance(r, DaxRejected))

    # --- the graph ----------------------------------------------------------------------------------
    graph = build_dax_measure_graph(FakeLlm.__call__.__get__(llm))
    llm.answers[NR] = [bad, NR_ANSWER]
    final = run_graph(graph, {"kpi": KPI_NR, "spec": spec, "measure": NR}, run_id="r", stage="dax_measure")
    check("graph path with a retry", final["node_path"] == ["propose_dax", "precheck_references", "propose_dax", "precheck_references"] and final["proposal"] is not None)
    llm.answers[NR] = [NR_ANSWER]
    final = run_graph(graph, {"kpi": KPI_NR, "spec": spec, "measure": NR}, run_id="r", stage="dax_measure")
    check("graph path without a retry", final["node_path"] == ["propose_dax", "precheck_references"])
    llm.answers[NR] = [bad]
    final = run_graph(graph, {"kpi": KPI_NR, "spec": spec, "measure": NR}, run_id="r", stage="dax_measure")
    check("graph is bounded to 2 attempts and ends without a proposal", final["attempt"] == 2 and final["proposal"] is None and len(final["node_path"]) == 4)

    check("description quotes the approved definition", approved_description(KPI_NR).splitlines()[:2]
          == ["Approved: Sum of line totals excluding cancelled orders", "Grain: order line"]
          and "Exclusions: Cancelled orders have Status = 6" in approved_description(KPI_NR))

    # --- the endpoint ---------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        api_llm = FakeLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_dax_llm] = lambda: api_llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(generated=True, kpis=(KPI_NR, KPI_MON), state=RunState.PLAN_APPROVED):
            run = store.create("Show net revenue by product")
            spec = spec_with_measures(run_id=run.run_id)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = t502.build_plan(spec)
            save_yaml(spec, run_dir / "ModelSpec.yaml")
            save_yaml(plan, run_dir / "ExecutionPlan.yaml")
            write_json(run_dir / "MetadataInventory.json", t502.build_inventory(spec).to_dict())
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Revenue", entities=["Sales"], kpis=list(kpis)), run_dir / "BusinessContract.yaml")
            run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
            for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
                run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.state = state
            store.save(run)
            if generated:
                assert client.post(f"/runs/{run.run_id}/model/generate").status_code == 200
            return run.run_id

        run_id = new_run()
        api_llm.answers = {NR: [NR_ANSWER], MON: [MON_ANSWER]}
        r = client.post(f"/runs/{run_id}/model/measures")
        body = r.json()
        by_name = {m["name"]: m for m in body["measures"]}
        check("POST measures: 200, both accepted, TMDL version 2", r.status_code == 200 and body["version"] == 2
              and by_name[NR] == {"name": NR, "dax": NR_DAX, "status": "accepted", "reason": None} and by_name[MON]["status"] == "accepted")
        saved = load_yaml(ModelSpec, runs_root / run_id / "ModelSpec.yaml")
        measures = {m.name: m for m in saved.measures}
        check("the ModelSpec holds the DAX and a description that quotes the definition",
              measures[NR].dax == NR_DAX and measures[NR].description.startswith("Approved: Sum of line totals excluding cancelled orders")
              and "Exclusions: Cancelled orders have Status = 6" in measures[NR].description and saved.version == 2)
        run = store.get(run_id)
        check("artifacts follow: ModelSpec v2 (still approved), TMDLModel v2, state unchanged",
              run.artifacts["ModelSpec"].version == 2 and run.artifacts["ModelSpec"].status == "approved"
              and run.artifacts["ModelSpec"].hash == canonical_hash(to_data(saved))
              and run.artifacts["TMDLModel"].version == 2 and run.state is RunState.MODEL_GENERATING
              and (runs_root / run_id / "plans" / "ModelSpec.v2.yaml").is_file())
        v2_sales = (runs_root / run_id / "model" / "v2" / "definition" / "tables" / "Sales.tmdl").read_text(encoding="utf-8")
        v1_sales = (runs_root / run_id / "model" / "v1" / "definition" / "tables" / "Sales.tmdl").read_text(encoding="utf-8")
        check("TMDL v2 has the measures and v1 is untouched",
              f"measure 'Net Revenue' = {NR_DAX}" in v2_sales and "/// Approved: Sum of line totals excluding cancelled orders" in v2_sales and "measure" not in v1_sales)

        calls_before = len(api_llm.calls)
        r = client.post(f"/runs/{run_id}/model/measures")
        check("an identical second call replays the saved answers: no model call, no new version",
              r.status_code == 200 and r.json()["version"] == 2 and len(api_llm.calls) == calls_before
              and not (runs_root / run_id / "model" / "v3").exists())

        bad_mon = answer(MON, "SUM(Date[Nope])", [col("Date", "Nope")])
        # one rejected measure doesn't stop the others
        run_id = new_run()
        api_llm.answers = {NR: [NR_ANSWER], MON: [bad_mon]}
        mon_calls = api_llm.count(MON)
        r = client.post(f"/runs/{run_id}/model/measures")
        by_name = {m["name"]: m for m in r.json()["measures"]}
        check("a rejected measure is reported with its reason, and the other is still accepted",
              r.status_code == 200 and by_name[MON]["status"] == "rejected" and "Date[Nope]" in by_name[MON]["reason"] and by_name[NR]["status"] == "accepted"
              and api_llm.count(MON) - mon_calls == 2 and r.json()["version"] == 2)
        saved = load_yaml(ModelSpec, runs_root / run_id / "ModelSpec.yaml")
        check("the rejected measure has no DAX", {m.name: m.dax for m in saved.measures}[MON] is None)
        tmdl = (runs_root / run_id / "model" / "v2" / "definition" / "tables" / "Sales.tmdl").read_text(encoding="utf-8")
        check("TMDL includes only the accepted measure", "measure 'Net Revenue'" in tmdl and "Monthly Revenue" not in tmdl)

        # all rejected: nothing changes
        run_id = new_run()
        api_llm.answers = {NR: [bad], MON: [bad_mon]}
        r = client.post(f"/runs/{run_id}/model/measures")
        check("all rejected: 200, no new ModelSpec or TMDL version", r.status_code == 200 and all(m["status"] == "rejected" for m in r.json()["measures"])
              and r.json()["version"] == 1 and not (runs_root / run_id / "model" / "v2").exists() and store.get(run_id).artifacts["ModelSpec"].version == 1)

        # model failures are per measure
        run_id = new_run()
        api_llm.answers = {NR: [NR_ANSWER], MON: [LlmTimeout("The model did not answer within 60 seconds")]}
        r = client.post(f"/runs/{run_id}/model/measures")
        by_name = {m["name"]: m for m in r.json()["measures"]}
        check("an LLM timeout rejects only that measure", by_name[MON]["status"] == "rejected" and "did not answer" in by_name[MON]["reason"] and by_name[NR]["status"] == "accepted")

        # a KPI that isn't in the contract
        run_id = new_run(kpis=(KPI_NR,))
        api_llm.answers = {NR: [NR_ANSWER], MON: [MON_ANSWER]}
        r = client.post(f"/runs/{run_id}/model/measures")
        by_name = {m["name"]: m for m in r.json()["measures"]}
        check("a measure whose KPI isn't in the contract is rejected", by_name[MON]["status"] == "rejected" and "not in the approved contract" in by_name[MON]["reason"])

        # states and 404s
        check("before the model is generated: 404", envelope(client.post(f"/runs/{new_run(generated=False, state=RunState.MODEL_GENERATING)}/model/measures"), 404, "NOT_FOUND"))
        check("wrong state: 409", envelope(_wrong_state(client, store, new_run()), 409, "WRONG_STATE"))
        check("unknown run: 404", envelope(client.post("/runs/nope/model/measures"), 404, "RUN_NOT_FOUND"))

        stale = new_run()
        path = runs_root / stale / "ModelSpec.yaml"
        path.write_bytes(path.read_bytes().replace(b"model_name: Sales Model", b"model_name: Changed"))
        check("a ModelSpec edited after approval: 409 STALE_VERSION", envelope(client.post(f"/runs/{stale}/model/measures"), 409, "STALE_VERSION"))
        app.dependency_overrides.clear()

    print("All checks passed.")


def _wrong_state(client, store, run_id):
    run = store.get(run_id)
    run.state = RunState.MODEL_APPROVED
    store.save(run)
    return client.post(f"/runs/{run_id}/model/measures")


if __name__ == "__main__":
    main()
