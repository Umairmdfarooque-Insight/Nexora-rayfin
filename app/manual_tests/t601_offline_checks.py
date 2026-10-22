"""T-601 offline checks: questions, plan validation, the report planner and `POST /runs/{id}/report/plan`.

Run: python -m app.manual_tests.t601_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The model is a fake that returns scripted answers, and the run and approved model are written by
hand into a temporary folder, so no sign-in or network is needed. A real model is in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_report_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, load_yaml, save_yaml, to_data
from app.contracts.report_plan import ReportPlan
from app.llm.errors import LlmInvalidOutput
from app.manual_tests import t502_offline_checks as t502
from app.report.plan_validation import validate_report_plan
from app.report.planner import derive_questions, propose_report_plan
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, utc_now
from app.workflow.states import RunState

check = t502.check
NR, UNK = "Net Revenue", "Customer Churn"


def ref(measure=None, table=None, column=None, hierarchy=None):
    return {"measure": measure, "table": table, "column": column, "hierarchy": hierarchy}


def visual(vid, vtype, title, questions, category=(), values=(), series=(), drill=None):
    return {
        "id": vid, "type": vtype, "title": title, "question_ids": list(questions),
        "fields": {"category": list(category), "series": list(series), "values": list(values)},
        "filters": [], "drill": drill,
    }


def answer(pages, interactions=(), unanswered=()):
    return json.dumps({"pages": pages, "interactions": list(interactions), "unanswered_questions": list(unanswered)})


def good_answer(visuals_per_page=2):
    return answer(
        [{
            "name": "Overview", "purpose": "Revenue", "filters": [],
            "visuals": [
                visual("a", "lineChart", "Net Revenue by Month", ["Q-R1"],
                       category=[ref(table="Date", hierarchy="Calendar")], values=[ref(measure=NR)], drill={"hierarchy": "Calendar", "table": "Date"}),
                visual("b", "clusteredBarChart", "Net Revenue by Product", ["Q-R2"],
                       category=[ref(table="Product", column="Product Name")], values=[ref(measure=NR)]),
            ][:visuals_per_page],
        }],
        interactions=[{"from_visual": "b", "to_visual": "a", "type": "filter"}],
    )


class FakeLlm:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, system, user, schema):
        self.calls.append(user)
        item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        return item


KPI = bc.Kpi(name=NR, formula_intent="Sum of line totals", grain="order line", currency="USD", time_logic="by month")
KPI_GAP = bc.Kpi(name=UNK, formula_intent="Customers lost", grain="customer")


def contract(run_id="run-1", kpis=(KPI,), dimensions=("Product",)):
    return bc.BusinessContract(
        run_id=run_id, business_objective="Understand revenue", entities=["Sales"], kpis=list(kpis), dimensions=list(dimensions),
        security=[bc.SecurityRequirement(description="Regional", applies_to=["Sales leadership"])],
    )


def main() -> None:
    spec = t502.build_spec(dax=True)
    c = contract()

    # --- the questions -----------------------------------------------------------------------------
    questions, forced = derive_questions(c, spec)
    check("questions: one per KPI (trend when it has time logic) and per dimension, with sources",
          [(q.id, q.source) for q in questions] == [("Q-R1", "contract.kpis[Net Revenue]"), ("Q-R2", "contract.dimensions[Product]")]
          and questions[0].text == "How does Net Revenue trend over time (by month)?" and forced == {})
    questions, forced = derive_questions(contract(kpis=(KPI, KPI_GAP)), spec)
    check("a KPI with no measure in the model is unanswered up front, naming the KPI",
          list(forced) == ["Q-R2"] and "Customer Churn" in forced["Q-R2"])
    no_dax = t502.build_spec(dax=False)
    check("a measure without DAX doesn't count", len(derive_questions(c, no_dax)[1]) == 2)

    # --- validation --------------------------------------------------------------------------------
    llm = FakeLlm(good_answer())
    plan = propose_report_plan(c, spec, llm, "run-1", "Sales")
    check("a good answer: 2 visuals, ids renumbered v1 v2, every question mapped, audience from the contract",
          [v.id for p in plan.pages for v in p.visuals] == ["v1", "v2"] and plan.audience == ["Sales leadership"]
          and plan.interactions[0].from_visual == "v2" and plan.interactions[0].to_visual == "v1" and not plan.unanswered_questions
          and len(llm.calls) == 1)
    payload = json.loads(llm.calls[0])
    check("the model sees the contract and names/types, no rows",
          {"business_objective", "kpis", "questions", "measures", "tables"} <= set(payload) and "rows" not in llm.calls[0])
    check("validation of the finished plan passes", validate_report_plan(plan, spec)[1].passed)

    data = plan.to_dict()
    data["pages"][0]["visuals"][0]["fields"]["values"] = [{"measure": "Net Revenue X"}]
    result = validate_report_plan(data, spec)[1]
    msgs = [p for ch in result.checks for p in ch.evidence.get("paths", [])]
    check("a bad measure: RPLAN.2 names the visual and the field", not result.passed and any(m.startswith("v1.") and "Net Revenue X" in m for m in msgs))
    data = plan.to_dict()
    data["pages"][0]["visuals"][1]["id"] = "v1"
    check("duplicate visual ids fail RPLAN.4", any(ch.id == "RPLAN.4" and ch.status == "fail" for ch in validate_report_plan(data, spec)[1].checks))
    data = plan.to_dict()
    data["pages"][0]["visuals"][0]["type"] = "pieChart"
    msg = next(p for ch in validate_report_plan(data, spec)[1].checks if ch.id == "RPLAN.5" for p in ch.evidence.get("paths", []))
    check("an unsupported type fails RPLAN.5 and lists the allowed ones", "pieChart" in msg and "lineChart" in msg and "slicer" in msg)
    data = plan.to_dict()
    data["pages"][0]["visuals"][1]["question_ids"] = []
    check("a question with no visual fails RPLAN.3", any(ch.id == "RPLAN.3" and ch.status == "fail" for ch in validate_report_plan(data, spec)[1].checks))
    data = plan.to_dict()
    data["pages"][0]["visuals"][0]["fields"]["category"] = [{"table": "Date", "hierarchy": "Fiscal"}]
    check("an unknown hierarchy fails RPLAN.2", any("Fiscal" in p for ch in validate_report_plan(data, spec)[1].checks for p in ch.evidence.get("paths", [])))
    data = plan.to_dict()
    data["interactions"][0]["to_visual"] = "v9"
    check("an interaction to a missing visual fails RPLAN.7", any(ch.id == "RPLAN.7" and ch.status == "fail" for ch in validate_report_plan(data, spec)[1].checks))
    data = plan.to_dict()
    data["pages"][0]["unknown_key"] = 1
    check("a typo'd key fails RPLAN.1", validate_report_plan(data, spec)[0] is None)
    data = plan.to_dict()
    data["pages"][0]["visuals"] = data["pages"][0]["visuals"] * 5
    check("more than 8 visuals on a page fails RPLAN.6", any(ch.id == "RPLAN.6" and ch.status == "fail" for ch in validate_report_plan(data, spec)[1].checks))
    data = plan.to_dict()
    data["pages"][0]["visuals"][0]["fields"]["values"] = [{"table": "Sales", "column": "LineTotal"}]
    check("a raw column in values is refused (no implicit measures)", not validate_report_plan(data, spec)[1].passed)

    # --- retry and invalid output ------------------------------------------------------------------
    bad = answer([{"name": "P", "purpose": "x", "filters": [], "visuals": [visual("a", "card", "Churn", ["Q-R1", "Q-R2"], values=[ref(measure=UNK)])]}])
    llm = FakeLlm(bad, good_answer())
    plan = propose_report_plan(c, spec, llm, "run-1")
    check("an invented measure: one retry carrying the error, then accepted", len(llm.calls) == 2 and UNK in llm.calls[1] and plan.pages[0].visuals[0].title == "Net Revenue by Month")
    llm = FakeLlm(bad)
    try:
        propose_report_plan(c, spec, llm, "run-1")
        failed = None
    except LlmInvalidOutput as exc:
        failed = exc
    check("unusable twice: LlmInvalidOutput after exactly 2 attempts", failed is not None and len(llm.calls) == 2 and len(failed.attempts) == 2)
    llm = FakeLlm(answer([{"name": "P", "purpose": "x", "filters": [], "visuals": [visual("a", "lineChart", "T", ["Q-R1"], category=[ref(table="Date", hierarchy="Calendar")], values=[ref(measure=NR)])]}],
                         unanswered=[{"question_id": "Q-R2", "reason": "No product measure"}]))
    plan = propose_report_plan(c, spec, llm, "run-1")
    check("a question the model flags with no visual is listed as unanswered", [u.question_id for u in plan.unanswered_questions] == ["Q-R2"])
    llm = FakeLlm("not json")
    try:
        propose_report_plan(c, spec, llm, "run-1")
        failed = None
    except LlmInvalidOutput:
        failed = True
    check("non-JSON output is unusable", failed)
    plan = propose_report_plan(contract(kpis=(KPI_GAP,), dimensions=()), spec, FakeLlm("never called"), "run-1")
    check("nothing answerable: no model call, no pages, all unanswered", not plan.pages and len(plan.unanswered_questions) == 1)

    # --- the endpoint ------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        api_llm = FakeLlm(good_answer())
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_report_llm] = lambda: api_llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(KPI,), state=RunState.MODEL_APPROVED, with_model=True):
            run = store.create("Show net revenue by product")
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(contract(run.run_id, kpis), run_dir / "BusinessContract.yaml")
            if with_model:
                model = t502.build_spec(run_id=run.run_id, dax=True)
                save_yaml(model, run_dir / "ModelSpec.yaml")
                run.artifacts["ModelSpec"] = ArtifactRef(path="ModelSpec.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
                run.artifacts["TMDLModel"] = ArtifactRef(path="model/v1", version=1, hash="sha256:tmdl", status="approved", updated_at=utc_now())
            run.artifacts["BusinessContract"] = ArtifactRef(path="BusinessContract.yaml", version=1, hash="sha256:contract", status="approved", updated_at=utc_now())
            run.state = state
            store.save(run)
            return run.run_id

        run_id = new_run()
        r = client.post(f"/runs/{run_id}/report/plan")
        body = r.json()
        check("POST report/plan: 200 with the plan and a passing validation",
              r.status_code == 200 and body["validation"]["passed"] and body["plan"]["version"] == 1
              and [v["id"] for v in body["plan"]["pages"][0]["visuals"]] == ["v1", "v2"])
        check("plan fields drop unused keys; based_on names the model and contract",
              body["plan"]["pages"][0]["visuals"][0]["fields"]["values"] == [{"measure": NR}]
              and body["plan"]["based_on"] == {"SemanticModel": "sha256:tmdl", "BusinessContract": "sha256:contract"})
        run = store.get(run_id)
        saved = json.loads((runs_root / run_id / "ReportPlan.json").read_text(encoding="utf-8"))
        check("ReportPlan.json, its v1 copy and the artifact exist; the state stays MODEL_APPROVED",
              saved == body["plan"] and (runs_root / run_id / "reports" / "ReportPlan.v1.json").is_file()
              and run.artifacts["ReportPlan"].status == "draft" and run.artifacts["ReportPlan"].hash == canonical_hash(saved) and run.state == RunState.MODEL_APPROVED)
        calls = len(api_llm.calls)
        r2 = client.post(f"/runs/{run_id}/report/plan")
        check("a repeat call makes no model call and no new version", r2.status_code == 200 and len(api_llm.calls) == calls and r2.json()["plan"]["version"] == 1)

        r = client.get(f"/runs/{run_id}/report/plan")
        check("GET report/plan returns the saved plan, valid", r.status_code == 200 and r.json()["validation"]["passed"])
        path = runs_root / run_id / "ReportPlan.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"Net Revenue"', '"Net Revenue X"', 1), encoding="utf-8")
        r = client.get(f"/runs/{run_id}/report/plan")
        failed_paths = [p for ch in r.json()["validation"]["checks"] for p in ch["evidence"].get("paths", [])]
        check("a hand edit to a missing measure fails re-validation, naming v1 and the field",
              r.status_code == 200 and not r.json()["validation"]["passed"] and any(p.startswith("v1.") and "Net Revenue X" in p for p in failed_paths))

        gap_run = new_run(kpis=(KPI, KPI_GAP))
        api_llm.answers = [good_answer().replace('"Q-R2"', '"Q-R3"')]
        r = client.post(f"/runs/{gap_run}/report/plan")
        body = r.json()
        gap = store.get(gap_run)
        check("a question no measure answers: listed as unanswered, artifact needs_input, run NEEDS_INPUT with resume MODEL_APPROVED",
              r.status_code == 200 and [u["question_id"] for u in body["plan"]["unanswered_questions"]] == ["Q-R2"]
              and gap.state == RunState.NEEDS_INPUT and gap.resume_state == RunState.MODEL_APPROVED
              and gap.artifacts["ReportPlan"].status == "needs_input" and gap.history[-1].to == RunState.NEEDS_INPUT)
        check("no measure was invented: the model spec is untouched",
              [m.name for m in load_yaml(ModelSpec, runs_root / gap_run / "ModelSpec.yaml").measures] == ["Net Revenue", "Order Count"])
        lines = (runs_root / gap_run / "data_gaps.jsonl").read_text(encoding="utf-8").splitlines()
        check("a data gap is recorded for the question", len(lines) == 1 and "Q-R2" in lines[0] and "Customer Churn" in lines[0])

        wrong = new_run(state=RunState.PLAN_APPROVED)
        check("wrong state: 409 WRONG_STATE", envelope(client.post(f"/runs/{wrong}/report/plan"), 409, "WRONG_STATE"))
        nomodel = new_run(with_model=False)
        check("no approved model: 404 NOT_FOUND", envelope(client.post(f"/runs/{nomodel}/report/plan"), 404, "NOT_FOUND"))
        check("an unknown run: 404", client.post("/runs/nope/report/plan").status_code == 404)
        check("GET before a plan: 404 NOT_FOUND", envelope(client.get(f"/runs/{wrong}/report/plan"), 404, "NOT_FOUND"))
        stale = new_run()
        p = runs_root / stale / "ModelSpec.yaml"
        p.write_bytes(p.read_bytes().replace(b"model_name: Sales Model", b"model_name: Changed"))
        check("a ModelSpec edited after approval: 409 STALE_VERSION", envelope(client.post(f"/runs/{stale}/report/plan"), 409, "STALE_VERSION"))

        invalid = new_run()
        api_llm.answers = [bad]
        check("unusable model output twice: 502 LLM_INVALID_OUTPUT and nothing saved",
              envelope(client.post(f"/runs/{invalid}/report/plan"), 502, "LLM_INVALID_OUTPUT")
              and not (runs_root / invalid / "ReportPlan.json").exists() and store.get(invalid).state == RunState.MODEL_APPROVED)
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
