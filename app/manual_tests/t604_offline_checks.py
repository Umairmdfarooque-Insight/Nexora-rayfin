"""T-604 offline checks: the report review, the file endpoint, the role approvals of the Report and
the regeneration of one visual or page.

Run: python -m app.manual_tests.t604_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI and the model are fakes, the roles are a temporary file, and the run is written by
hand into a temporary folder, so no sign-in or network is needed. A real workspace and the Rayfin
screen are in MANUAL_TESTING.md.
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
    get_report_llm,
    get_report_services,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import save_yaml, to_data
from app.contracts.report_plan import ReportPlan
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t602_offline_checks as t602
from app.manual_tests import t603_offline_checks as t603
from app.repair.report_adapter import read_tree
from app.store.file_store import FileRunStore
from app.workflow import roles as roles_module
from app.workflow.harvest_stage import canonical_hash
from app.workflow.report_review import ReportServices
from app.workflow.run import ArtifactRef, utc_now
from app.workflow.states import RunState

check = t502.check
SANDBOX = t602.SANDBOX
REVIEWER, OWNER, BOTH, NOBODY = "rev@contoso.com", "owner@contoso.com", "both@contoso.com", "nobody@contoso.com"


def column_chart_answer(question_ids=("Q-R1",)) -> str:
    """The model's replacement for v1: a column chart of the same data."""
    return json.dumps(
        {
            "visual": t601.visual(
                "x", "clusteredColumnChart", "Net Revenue by Month (columns)", list(question_ids),
                category=[t601.ref(table="Date", hierarchy="Calendar")], values=[t601.ref(measure=t601.NR)], drill={"hierarchy": "Calendar", "table": "Date"},
            )
        }
    )


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        roles_file = tmp / "roles.yaml"
        roles_file.write_text(f"roles:\n  project_reviewer: [{REVIEWER}, {BOTH}]\n  business_owner: [{OWNER}, {BOTH}]\n", encoding="utf-8")
        original_roles = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        runs_root = tmp / "runs"
        store = FileRunStore(runs_root)
        plan_llm = t601.FakeLlm(t601.good_answer())
        items, pbi = t602.FakeItems(), t603.FakePbi()
        regen_llm = t601.FakeLlm(column_chart_answer())
        who = {"upn": BOTH}
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_report_llm] = lambda: plan_llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        app.dependency_overrides[get_report_services] = lambda: ReportServices(llm=regen_llm, items=lambda: items, pbi=lambda: pbi)
        for dependency in (get_sql_connector_factory, get_dax_llm, get_recon_llm):
            app.dependency_overrides[dependency] = lambda: None
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o-" + who["upn"], upn=who["upn"], name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(validate=True):
            run = store.create("Show net revenue by product")
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(t601.contract(run.run_id), run_dir / "BusinessContract.yaml")
            model = t502.build_spec(run_id=run.run_id, dax=True)
            save_yaml(model, run_dir / "ModelSpec.yaml")
            run.artifacts["ModelSpec"] = ArtifactRef(path="ModelSpec.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.artifacts["TMDLModel"] = ArtifactRef(path="model/v1", version=1, hash="sha256:tmdl", status="approved", updated_at=utc_now())
            run.artifacts["BusinessContract"] = ArtifactRef(path="BusinessContract.yaml", version=1, hash="sha256:contract", status="approved", updated_at=utc_now())
            run.state = RunState.MODEL_APPROVED
            store.save(run)
            assert client.post(f"/runs/{run.run_id}/report/plan").status_code == 200
            path = run_dir / "ReportPlan.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["pages"][0]["filters"] = [{"table": "Date", "column": "Year", "values": [2024]}]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            assert client.post(f"/runs/{run.run_id}/report/generate").status_code == 200
            if validate:
                assert client.post(f"/runs/{run.run_id}/report/validate").json()["report"]["passed"]
            return run.run_id

        def review(run_id):
            return client.get(f"/runs/{run_id}/report/review").json()

        def approve(run_id, action="ACCEPT", role="project_reviewer", comment=None, expected=None, **extra):
            body = {"artifact": "Report", "action": action, "expected_hash": expected or review(run_id)["report_hash"], "role": role, **extra}
            if comment:
                body["comment"] = comment
            return client.post(f"/runs/{run_id}/approvals", json=body)

        def statuses(run_id):
            return {s["role"]: s["status"] for s in review(run_id)["approvals_status"]}

        # --- the review ------------------------------------------------------------------------------------------
        rid = new_run(validate=False)
        data = review(rid)
        page = data["pages"][0]
        check("review before validation: the page and its two visuals, with titles and the questions they answer, statuses not validated",
              [p["name"] for p in data["pages"]] == ["p1"] and page["display_name"] == "Overview" and [v["id"] for v in page["visuals"]] == ["v1", "v2"]
              and page["visuals"][0]["title"] == "Net Revenue by Month" and page["visuals"][0]["questions"][0]["id"] == "Q-R1"
              and "trend" in page["visuals"][0]["questions"][0]["text"] and page["visuals"][0]["bind_status"] == "not_validated" and page["visuals"][0]["query_status"] == "not_validated")
        check("no render yet: no image, an open-in-Power-BI link, and Gate G5 not passed (not validated)",
              page["image_url"] is None and page["open_url"] == f"https://app.powerbi.com/groups/{SANDBOX}/reports/report-1"
              and data["gate"]["passed"] is False and data["gate"]["checks"][0]["id"] == "G5.VALIDATED" and data["validation_current"] is False)
        check("the required roles are the project reviewer and the business owner, both pending",
              data["approvals_required"] == ["project_reviewer", "business_owner"] and statuses(rid) == {"project_reviewer": "pending", "business_owner": "pending"})
        check("approving before validation: 409 GATE_FAILED", envelope(approve(rid), 409, "GATE_FAILED"))

        client.post(f"/runs/{rid}/report/validate")
        data = review(rid)
        page = data["pages"][0]
        check("after validation: bind and query statuses pass, the page has a render, Gate G5 passes and is current",
              all(v["bind_status"] == "pass" and v["query_status"] == "pass" for v in page["visuals"]) and page["render_status"] == "pass"
              and page["image_url"] == f"/runs/{rid}/files/report/v1/renders/p1.png" and data["gate"]["passed"] and data["validation_current"])
        image = client.get(page["image_url"])
        check("the render is served by the file endpoint", image.status_code == 200 and image.headers["content-type"] == "image/png" and image.content == t603.PNG + b"p1")

        # --- the file endpoint ----------------------------------------------------------------------------------------
        (runs_root / "secret.txt").write_text("outside", encoding="utf-8")
        blocked = ["..%2F..%2F.env", "..%2Fsecret.txt", "%2E%2E%2Fsecret.txt", "..%5Csecret.txt", "%2Fetc%2Fpasswd", "C%3A%2FWindows%2Fwin.ini", "report%2Fv1%2F..%2F..%2F..%2Fsecret.txt", "report", "report%2Fv1%2Frenders", "nothing.png"]
        check("traversal and absolute paths, directories and missing files are all 404",
              all(client.get(f"/runs/{rid}/files/{p}").status_code == 404 for p in blocked))
        check("a path with a literal .. segment is 404 too", client.get(f"/runs/{rid}/files/report/v1/../../../secret.txt").status_code == 404)
        check("another run's folder isn't reachable through this run", client.get(f"/runs/{rid}/files/..%2F{rid}%2FBusinessContract.yaml").status_code == 404
              and client.get(f"/runs/{rid}/files/BusinessContract.yaml").status_code == 200)
        check("an unknown run: 404", client.get("/runs/nope/files/x.png").status_code == 404)

        # --- the placeholder when the export isn't available -------------------------------------------------------------------
        pbi.export_mode = "unavailable"
        nr = new_run()
        page = review(nr)["pages"][0]
        check("export unavailable: no image, a warning message and the open-in-Power-BI link for the placeholder",
              page["image_url"] is None and page["render_status"] == "warning" and "export unavailable" in page["render_message"] and page["open_url"])
        pbi.export_mode = "ok"

        # --- approvals -------------------------------------------------------------------------------------------------
        who["upn"] = NOBODY
        check("a user with neither role: 403", envelope(approve(rid), 403, "FORBIDDEN"))
        who["upn"] = REVIEWER
        check("a project reviewer can't approve as the business owner: 403", envelope(approve(rid, role="business_owner"), 403, "FORBIDDEN"))
        check("a role that isn't required: 422", approve(rid, role="bi_engineer").status_code == 422)
        check("a reject without a comment: 422", approve(rid, action="REJECT").status_code == 422)
        check("a stale hash: 409 STALE_VERSION", envelope(approve(rid, expected="sha256:old"), 409, "STALE_VERSION"))
        r = approve(rid)
        check("the project reviewer approves: the run stays REPORT_GENERATING, the other role is pending",
              r.status_code == 200 and r.json()["run"]["state"] == "REPORT_GENERATING" and statuses(rid) == {"project_reviewer": "approved", "business_owner": "pending"}
              and r.json()["gate_result"]["passed"])
        check("the same role can't approve the same version twice: 409", envelope(approve(rid), 409, "ALREADY_APPROVED"))

        # a change to the report invalidates the earlier approval
        report_file = runs_root / rid / "report" / "v1" / "definition" / "pages" / "p1" / "page.json"
        report_file.write_text(report_file.read_text(encoding="utf-8").replace("Overview", "Overview "), encoding="utf-8")
        data = review(rid)
        check("a report that changed after a partial approval: the approval is invalidated and marked so in the history",
              statuses(rid)["project_reviewer"] == "invalidated" and any(a["note"] == "invalidated: report changed" and a["current"] is False for a in data["approvals_done"]))
        who["upn"] = OWNER
        check("the validation is of another hash: the business owner's approval is 409 GATE_FAILED (G5.CURRENT)",
              envelope(approve(rid, role="business_owner"), 409, "GATE_FAILED"))
        check("a hash from before the change: 409 STALE_VERSION", envelope(approve(rid, role="business_owner", expected=data["approvals_done"][0]["artifact_hash"]), 409, "STALE_VERSION"))
        client.post(f"/runs/{rid}/report/validate")  # validates the files as they are now
        who["upn"] = REVIEWER
        check("after validating again the project reviewer has to approve again (invalidated -> approved)",
              approve(rid).status_code == 200 and statuses(rid)["project_reviewer"] == "approved")
        who["upn"] = OWNER
        r = approve(rid, role="business_owner")
        run = store.get(rid)
        check("the business owner approves: REPORT_APPROVED, PbirReport and ReportPlan approved",
              r.status_code == 200 and run.state == RunState.REPORT_APPROVED and run.artifacts["PbirReport"].status == "approved" and run.artifacts["ReportPlan"].status == "approved")
        records = [json.loads(line) for line in (runs_root / rid / "approvals.jsonl").read_text(encoding="utf-8").splitlines()]
        report_records = [x for x in records if x["artifact"] == "Report"]
        check("two current approval records, one per role, with the user and the report hash",
              sorted(x["role"] for x in report_records if x["artifact_hash"] == review(rid)["report_hash"] and x["action"] == "ACCEPT") == ["business_owner", "project_reviewer"]
              and all(x["user"]["upn"] for x in report_records))
        check("a second Accept of the approved report: 409 ALREADY_APPROVED", envelope(approve(rid, role="business_owner"), 409, "ALREADY_APPROVED"))
        who["upn"] = BOTH

        # one user with both roles approves each separately
        both = new_run()
        r1 = approve(both, role="project_reviewer")
        check("one user holding both roles approves one role at a time", r1.json()["run"]["state"] == "REPORT_GENERATING")
        check("then the other: REPORT_APPROVED", approve(both, role="business_owner").json()["run"]["state"] == "REPORT_APPROVED")

        # a reject is recorded and changes nothing else
        rej = new_run()
        r = approve(rej, action="REJECT", role="business_owner", comment="Needs a regional page")
        check("a reject needs the comment and is that role's decision: the run stays, the role shows rejected",
              r.status_code == 200 and r.json()["run"]["state"] == "REPORT_GENERATING" and statuses(rej)["business_owner"] == "rejected")

        # wrong state
        wrong = new_run()
        run = store.get(wrong)
        run.state = RunState.REPORT_FAILED
        store.save(run)
        check("approving in another state: 409 WRONG_STATE", envelope(approve(wrong), 409, "WRONG_STATE"))

        # --- regenerating one visual -------------------------------------------------------------------------------------
        regen = new_run()
        before = read_tree(runs_root / regen / "report" / "v1")
        who["upn"] = REVIEWER
        approve(regen, role="project_reviewer")
        pbi.exports.clear()
        r = approve(regen, action="REGENERATE", role="project_reviewer", comment="Use a column chart", target={"page": "Overview", "visual": "v1"})
        body = r.json()
        after = read_tree(runs_root / regen / "report" / "v2")
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        check("regenerate v1 with 'use a column chart': 200, and only v1's visual.json changes between the versions",
              r.status_code == 200 and changed == ["definition/pages/p1/visuals/v1/visual.json"]
              and json.loads((runs_root / regen / "report" / "v2" / changed[0]).read_text(encoding="utf-8"))["visual"]["visualType"] == "clusteredColumnChart")
        record = body["approval"]
        run = store.get(regen)
        latest = json.loads((runs_root / regen / "ValidationReport.json").read_text(encoding="utf-8"))["runs"][-1]
        check("Gate G5 ran again on the new version, the image was refreshed, the run is still REPORT_GENERATING",
              latest["artifact"] == "Report" and latest["artifact_version"] == 2 and latest["passed"] and run.state == RunState.REPORT_GENERATING
              and pbi.exports == ["p1"] and review(regen)["pages"][0]["image_url"].endswith("/report/v2/renders/p1.png"))
        check("the record names the target and the guidance; the plan is the next version, with the new visual type",
              record["action"] == "REGENERATE" and record["target"] == {"page": "Overview", "visual": "v1"} and record["comment"] == "Use a column chart"
              and json.loads((runs_root / regen / "ReportPlan.json").read_text(encoding="utf-8"))["version"] == 2
              and json.loads((runs_root / regen / "ReportPlan.json").read_text(encoding="utf-8"))["pages"][0]["visuals"][0]["type"] == "clusteredColumnChart")
        check("the earlier approval is invalidated by the new report hash", statuses(regen)["project_reviewer"] == "invalidated")

        # --- the cases that don't regenerate -----------------------------------------------------------------------------------
        calls = len(regen_llm.calls)
        check("a target that doesn't exist: 404", envelope(approve(regen, action="REGENERATE", comment="x", target={"page": "Overview", "visual": "v9"}), 404, "NOT_FOUND"))
        check("a page that doesn't exist: 404", envelope(approve(regen, action="REGENERATE", comment="x", target={"page": "Nowhere"}), 404, "NOT_FOUND"))
        check("a regenerate without a target or without guidance: 422",
              approve(regen, action="REGENERATE", comment="x").status_code == 422 and approve(regen, action="REGENERATE", target={"visual": "v1"}).status_code == 422)
        who["upn"] = NOBODY
        check("a regenerate by someone without either role: 403", envelope(approve(regen, action="REGENERATE", comment="x", target={"visual": "v1"}), 403, "FORBIDDEN"))
        who["upn"] = REVIEWER
        before_plan = (runs_root / regen / "ReportPlan.json").read_text(encoding="utf-8")
        regen_llm.answers = [json.dumps({"visual": t601.visual("x", "card", "Bad", ["Q-R1"], values=[t601.ref(measure="Invented Measure")])})]
        r = approve(regen, action="REGENERATE", comment="Make it a card of an invented thing", target={"visual": "v1"})
        check("a replacement that binds an invented measure is retried once, then refused: 502, the plan and the report are unchanged",
              envelope(r, 502, "LLM_INVALID_OUTPUT") and len(regen_llm.calls) == calls + 2
              and (runs_root / regen / "ReportPlan.json").read_text(encoding="utf-8") == before_plan and not (runs_root / regen / "report" / "v3").exists())
        regen_llm.answers = [column_chart_answer()]

        # --- regenerating a page ------------------------------------------------------------------------------------------
        page_run = new_run()
        page_answer = json.dumps(
            {"page": {"name": "ignored", "purpose": "One chart", "filters": [], "visuals": [
                t601.visual("a", "lineChart", "Net Revenue by Month", ["Q-R1", "Q-R2"], category=[t601.ref(table="Date", hierarchy="Calendar")], values=[t601.ref(measure=t601.NR)], drill={"hierarchy": "Calendar", "table": "Date"})]}}
        )
        regen_llm.answers = [page_answer]
        before = read_tree(runs_root / page_run / "report" / "v1")
        r = approve(page_run, action="REGENERATE", role="project_reviewer", comment="Only the monthly trend", target={"page": "Overview"})
        after = read_tree(runs_root / page_run / "report" / "v2")
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        check("regenerate a page: 200; only that page's files change, the second visual's folder is removed and its interaction dropped",
              r.status_code == 200 and all(n.startswith("definition/pages/p1/") for n in changed) and "definition/pages/p1/visuals/v2/visual.json" in changed
              and "definition/pages/p1/visuals/v2/visual.json" not in after and "visualInteractions" not in json.loads(after["definition/pages/p1/page.json"]))
        check("the page's name in the plan is kept, whatever the model wrote", json.loads((runs_root / page_run / "ReportPlan.json").read_text(encoding="utf-8"))["pages"][0]["name"] == "Overview")

        # --- a model defect found while regenerating goes back to the model -----------------------------------------------------
        defect = new_run()
        regen_llm.answers = [column_chart_answer()]
        pbi.broken = {"Net Revenue"}
        r = approve(defect, action="REGENERATE", role="project_reviewer", comment="Use a column chart", target={"visual": "v1"})
        check("a regenerated visual whose query fails in the model: the run goes back to MODEL_GENERATING with the reason",
              r.status_code == 200 and store.get(defect).state == RunState.MODEL_GENERATING and "Model defect" in store.get(defect).history[-1].reason)
        pbi.broken = set()

        roles_module.CONFIG_PATH = original_roles
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
