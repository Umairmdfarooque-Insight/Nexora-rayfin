"""T-602 offline checks: the PBIR generator, `FabricItemsClient.upsert_report` and
`POST /runs/{id}/report/generate`.

Run: python -m app.manual_tests.t602_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric is replaced by fakes and the run, plan and approved model are written by hand into a
temporary folder, so no sign-in or network is needed. Opening the report in a real sandbox
workspace is in MANUAL_TESTING.md.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_items_client, get_report_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import save_yaml, to_data
from app.contracts.report_plan import Filter, ReportPlan
from app.deploy.fabric_items import FabricItemsClient, FabricOperationFailed, NameConflict, TargetNotAllowed, pbir_parts
from app.manual_tests import t501_offline_checks as t501
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t601_offline_checks as t601
from app.report.pbir_generator import PbirGenerationError, check_pbir, generate_pbir
from app.report.planner import propose_report_plan
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash
from app.workflow.model_stage import folder_hash
from app.workflow.run import ArtifactRef, utc_now
from app.workflow.states import RunState

check = t502.check
SANDBOX = "44444444-4444-4444-8444-444444444444"


def read(folder: Path, rel: str) -> dict:
    return json.loads((folder / rel).read_text(encoding="utf-8"))


def make_plan(spec, run_id="run-1") -> ReportPlan:
    plan = propose_report_plan(t601.contract(run_id), spec, t601.FakeLlm(t601.good_answer()), run_id, "Sales")
    plan.pages[0].filters = [Filter(table="Date", column="Year", values=[2024])]
    plan.based_on = {"SemanticModel": "sha256:tmdl", "BusinessContract": "sha256:contract"}
    return plan


class FakeItems:
    """The slice of FabricItemsClient the stage uses."""

    def __init__(self):
        self.model_id = "model-1"
        self.deployed = True
        self.reports: dict[str, str] = {}  # name -> item id
        self.calls: list[tuple] = []
        self.fail: Exception | None = None

    def get_item_by_name(self, workspace_id, name, item_type):
        assert workspace_id == SANDBOX and item_type == "SemanticModel"
        return {"id": self.model_id, "displayName": name, "type": item_type} if self.deployed else None

    def upsert_report(self, workspace_id, name, pbir_dir):
        assert workspace_id == SANDBOX
        if self.fail:
            raise self.fail
        self.calls.append((workspace_id, name, folder_hash(pbir_dir), read(Path(pbir_dir), "definition.pbir")))
        operation = "updated" if name in self.reports else "created"
        self.reports.setdefault(name, f"report-{len(self.reports) + 1}")
        return {"item_id": self.reports[name], "operation": operation}


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    spec = t502.build_spec(dax=True)
    plan = make_plan(spec)

    # --- the generator -----------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files = generate_pbir(plan, "model-1", tmp / "a", spec)
        rels = [p.relative_to(tmp / "a").as_posix() for p in files]
        check("layout: definition.pbir, report, version, pages.json, page.json and one visual.json per visual",
              rels == sorted(rels) and rels == [
                  "definition.pbir", "definition/pages/p1/page.json", "definition/pages/p1/visuals/v1/visual.json",
                  "definition/pages/p1/visuals/v2/visual.json", "definition/pages/pages.json", "definition/report.json", "definition/version.json"])
        pbir = read(tmp / "a", "definition.pbir")
        check("definition.pbir binds by connection to the model id",
              pbir["datasetReference"]["byConnection"]["connectionString"] == "semanticmodelid=model-1" and pbir["version"] == "4.0" and "$schema" in pbir)
        check("every JSON file declares its $schema and passes the structural check", check_pbir(tmp / "a") == []
              and all("$schema" in json.loads(p.read_text(encoding="utf-8")) for p in files))
        check("files are UTF-8 with LF line endings and a final newline", all(b"\r" not in p.read_bytes() and p.read_bytes().endswith(b"}\n") for p in files))

        generate_pbir(plan, "model-1", tmp / "b", spec)
        check("deterministic: the same plan and model id give byte-identical folders", folder_hash(tmp / "a") == folder_hash(tmp / "b"))
        generate_pbir(plan, "model-2", tmp / "c", spec)
        diff = [p.relative_to(tmp / "a").as_posix() for p in files if (tmp / "c" / p.relative_to(tmp / "a")).read_bytes() != p.read_bytes()]
        check("a changed model id only changes definition.pbir", diff == ["definition.pbir"])

        v1 = read(tmp / "a", "definition/pages/p1/visuals/v1/visual.json")
        state = v1["visual"]["query"]["queryState"]
        levels = [(p["field"]["HierarchyLevel"]["Level"], p["active"]) for p in state["Category"]["projections"]]
        check("v1 is a lineChart: the Calendar hierarchy gives Year (shown), Quarter and Month (drill), Net Revenue is the Y measure",
              v1["name"] == "v1" and v1["visual"]["visualType"] == "lineChart" and levels == [("Year", True), ("Quarter", False), ("Month", False)]
              and state["Y"]["projections"][0]["field"]["Measure"] == {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Net Revenue"})
        v2 = read(tmp / "a", "definition/pages/p1/visuals/v2/visual.json")
        check("v2: a bar chart with the Product Name column on Category",
              v2["visual"]["visualType"] == "clusteredBarChart"
              and v2["visual"]["query"]["queryState"]["Category"]["projections"][0]["field"]["Column"]["Property"] == "Product Name")
        check("the visual title is the plan's title",
              v1["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"] == "'Net Revenue by Month'")
        page = read(tmp / "a", "definition/pages/p1/page.json")
        cond = page["filterConfig"]["filters"][0]["filter"]["Where"][0]["Condition"]["In"]
        check("the 2024 page filter is a categorical filter on Date[Year] with the literal 2024L",
              page["displayName"] == "Overview" and page["filterConfig"]["filters"][0]["field"]["Column"]["Property"] == "Year"
              and cond["Values"] == [[{"Literal": {"Value": "2024L"}}]])
        check("the interaction is written on its page", page["visualInteractions"] == [{"source": "v2", "target": "v1", "type": "DataFilter"}])
        check("pages.json lists the pages in order", read(tmp / "a", "definition/pages/pages.json")["pageOrder"] == ["p1"])

        # failures write nothing and name the visual
        bad = plan.model_copy(deep=True)
        bad.pages[0].visuals[0].type = "pieChart"
        err = None
        try:
            generate_pbir(bad, "model-1", tmp / "bad", spec)
        except PbirGenerationError as exc:
            err = str(exc)
        check("an unsupported visual type fails naming the visual, before any file is written",
              err is not None and err.startswith("v1:") and "pieChart" in err and not (tmp / "bad").exists())
        bad = plan.model_copy(deep=True)
        bad.pages[0].visuals[0].fields.category = []
        err = None
        try:
            generate_pbir(bad, "model-1", tmp / "bad", spec)
        except PbirGenerationError as exc:
            err = str(exc)
        check("a drill hierarchy that isn't a category field fails naming the visual", err is not None and err.startswith("v1:") and "drill" in err)
        bad = plan.model_copy(deep=True)
        bad.pages[0].visuals[0].fields.values[0].measure = "Net Revenue X"
        err = None
        try:
            generate_pbir(bad, "model-1", tmp / "bad", spec)
        except PbirGenerationError as exc:
            err = str(exc)
        check("a measure missing from the ModelSpec fails naming the visual and the measure", err is not None and "v1." in err and "Net Revenue X" in err)
        bad = plan.model_copy(deep=True)
        bad.interactions[0].to_visual = "v9"
        check("an interaction to a missing visual fails", _fails(bad, spec, tmp))

        # a card, a slicer, a table, a pivot and 8 visuals on a page
        many = plan.model_copy(deep=True)
        template = many.pages[0].visuals[1]
        many.pages[0].visuals = []
        for n in range(1, 9):
            v = template.model_copy(deep=True)
            v.id = f"v{n}"
            many.pages[0].visuals.append(v)
        many.pages[0].visuals[0].type = "card"
        many.pages[0].visuals[0].fields.category = []
        many.pages[0].visuals[0].drill = None
        many.pages[0].visuals[1].type = "slicer"
        many.pages[0].visuals[1].fields.values = []
        many.pages[0].visuals[2].type = "tableEx"
        many.pages[0].visuals[3].type = "pivotTable"
        many.interactions = []
        generate_pbir(many, "model-1", tmp / "many", spec)
        boxes = [read(tmp / "many", f"definition/pages/p1/visuals/v{n}/visual.json")["position"] for n in range(1, 9)]
        inside = all(b["x"] >= 0 and b["y"] >= 0 and b["x"] + b["width"] <= 1280 and b["y"] + b["height"] <= 720 for b in boxes)
        overlap = any(
            a is not b and a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
            for a in boxes for b in boxes
        )
        roles = {n: sorted(read(tmp / "many", f"definition/pages/p1/visuals/v{n}/visual.json")["visual"]["query"]["queryState"]) for n in (1, 2, 3, 4)}
        check("8 visuals fit the page without overlapping", inside and not overlap)
        check("query roles by type: card Values, slicer Values, table Values, pivot Rows+Values",
              roles == {1: ["Values"], 2: ["Values"], 3: ["Values"], 4: ["Rows", "Values"]})
        check("a card with no measure fails", _fails(_edit(plan, lambda p: (setattr(p.pages[0].visuals[0], "type", "card"), setattr(p.pages[0].visuals[0].fields, "values", []), setattr(p.pages[0].visuals[0], "drill", None))), spec, tmp))

        broken = tmp / "broken"
        shutil.copytree(tmp / "a", broken)
        (broken / "definition/version.json").write_text('{"$schema": "x"}', encoding="utf-8")
        shutil.rmtree(broken / "definition/pages/p1/visuals/v2")
        problems = check_pbir(broken)
        check("check_pbir reports a wrong $schema, a missing required key and a page folder without its visual file",
              any("version.json: $schema" in p for p in problems) and any("'version' is missing" in p for p in problems))

    # --- upsert_report -----------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "r"
        generate_pbir(plan, "model-1", folder, spec)
        parts = pbir_parts(folder)
        check("pbir_parts: definition.pbir first, then definition/** with base64 payloads",
              parts[0]["path"] == "definition.pbir" and [p["path"] for p in parts[1:]] == sorted(p["path"] for p in parts[1:])
              and all(p["payloadType"] == "InlineBase64" for p in parts))

        class Http(t501.FakeFabric):
            def __call__(self, method, url, headers=None, json=None, params=None, timeout=None):
                self.calls.append((method, url, json))
                if method == "GET" and url.endswith("/items"):
                    return t501.Resp(200, {"value": list(self.items)})
                if method == "POST" and url.endswith("/reports"):
                    item = {"id": "rep-1", "displayName": json["displayName"], "type": "Report"}
                    self.items.append(item)
                    return t501.Resp(201, item)
                if method == "POST" and url.endswith("/updateDefinition"):
                    return t501.Resp(200, {})
                raise AssertionError(f"unexpected call {method} {url}")

        os.environ["SANDBOX_WORKSPACE_ID"] = t501.SANDBOX
        fake = Http()
        client, _ = t501.make_client(fake)
        first = client.upsert_report(t501.SANDBOX, "Nexora_x_Report", folder)
        create = next(c for c in fake.posts() if c[1].endswith("/reports"))
        check("first publish creates the report with a PBIR definition",
              first == {"item_id": "rep-1", "operation": "created"} and create[2]["definition"]["format"] == "PBIR"
              and create[2]["displayName"] == "Nexora_x_Report" and create[2]["definition"]["parts"][0]["path"] == "definition.pbir")
        second = client.upsert_report(t501.SANDBOX, "nexora_x_report", folder)
        check("publishing again updates the same report: operation updated, one report in the workspace",
              second == {"item_id": "rep-1", "operation": "updated"} and len(fake.items) == 1 and fake.posts()[-1][1].endswith("/reports/rep-1/updateDefinition"))
        check("a workspace other than the sandbox is refused", t501.raises(TargetNotAllowed, client.upsert_report, t501.OTHER, "X", folder) is not None)
        fake.items.append({"id": "m", "displayName": "Taken", "type": "SemanticModel"})
        check("a same-named item of another type is a NameConflict", t501.raises(NameConflict, client.upsert_report, t501.SANDBOX, "Taken", folder) is not None)

    # --- the endpoint ----------------------------------------------------------------------------------------
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        llm = t601.FakeLlm(t601.good_answer())
        items = FakeItems()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_report_llm] = lambda: llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(t601.KPI,), state=RunState.MODEL_APPROVED, planned=True):
            run = store.create("Show net revenue by product")
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(t601.contract(run.run_id, kpis), run_dir / "BusinessContract.yaml")
            model = t502.build_spec(run_id=run.run_id, dax=True)
            save_yaml(model, run_dir / "ModelSpec.yaml")
            run.artifacts["ModelSpec"] = ArtifactRef(path="ModelSpec.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.artifacts["TMDLModel"] = ArtifactRef(path="model/v1", version=1, hash="sha256:tmdl", status="approved", updated_at=utc_now())
            run.artifacts["BusinessContract"] = ArtifactRef(path="BusinessContract.yaml", version=1, hash="sha256:contract", status="approved", updated_at=utc_now())
            run.state = RunState.MODEL_APPROVED
            store.save(run)
            if planned:
                assert client.post(f"/runs/{run.run_id}/report/plan").status_code == 200
            run = store.get(run.run_id)
            run.state = state
            store.save(run)
            return run.run_id

        run_id = new_run()
        r = client.post(f"/runs/{run_id}/report/generate")
        body = r.json()
        run = store.get(run_id)
        check("POST report/generate: 200 with version 1, the files, the report item id and operation created",
              r.status_code == 200 and body["version"] == 1 and body["report_item_id"] == "report-1" and body["operation"] == "created"
              and "definition.pbir" in body["files"] and "definition/pages/p1/visuals/v1/visual.json" in body["files"])
        check("the run moves MODEL_APPROVED -> REPORT_GENERATING and PbirReport v1 is registered, based on the plan and the model",
              run.state == RunState.REPORT_GENERATING and body["run"]["state"] == "REPORT_GENERATING"
              and run.artifacts["PbirReport"].version == 1 and run.artifacts["PbirReport"].status == "draft"
              and set(run.artifacts["PbirReport"].based_on) == {"ReportPlan", "SemanticModel"}
              and run.artifacts["PbirReport"].hash == folder_hash(runs_root / run_id / "report" / "v1"))
        publish = json.loads((runs_root / run_id / "ReportPublish.json").read_text(encoding="utf-8"))
        check("ReportPublish.json records the workspace, report and model ids",
              publish["workspace_id"] == SANDBOX and publish["report_item_id"] == "report-1" and publish["semantic_model_item_id"] == "model-1")
        check("the files on disk validate and bind to the deployed model id", check_pbir(runs_root / run_id / "report" / "v1") == []
              and items.calls[0][3]["datasetReference"]["byConnection"]["connectionString"] == "semanticmodelid=model-1")

        r2 = client.post(f"/runs/{run_id}/report/generate")
        check("generating again in REPORT_GENERATING: v2, operation updated, still one report",
              r2.status_code == 200 and r2.json()["version"] == 2 and r2.json()["operation"] == "updated" and len(items.reports) == 1
              and r2.json()["report_item_id"] == "report-1")
        check("v1 and v2 are identical", folder_hash(runs_root / run_id / "report" / "v1") == folder_hash(runs_root / run_id / "report" / "v2"))
        items.model_id = "model-2"
        r3 = client.post(f"/runs/{run_id}/report/generate")
        check("a redeployed model (new id) is rebound: v3 differs from v2 only in definition.pbir",
              r3.status_code == 200 and "model-2" in (runs_root / run_id / "report" / "v3" / "definition.pbir").read_text(encoding="utf-8")
              and [p.relative_to(runs_root / run_id / "report" / "v3").as_posix() for p in (runs_root / run_id / "report" / "v3").rglob("*")
                   if p.is_file() and p.read_bytes() != (runs_root / run_id / "report" / "v2" / p.relative_to(runs_root / run_id / "report" / "v3")).read_bytes()] == ["definition.pbir"])
        items.model_id = "model-1"

        wrong = new_run(state=RunState.PLAN_APPROVED)
        check("wrong state: 409 WRONG_STATE", envelope(client.post(f"/runs/{wrong}/report/generate"), 409, "WRONG_STATE"))
        unplanned = new_run(planned=False)
        check("no report plan: 404 NOT_FOUND", envelope(client.post(f"/runs/{unplanned}/report/generate"), 404, "NOT_FOUND"))
        items.deployed = False
        undeployed = new_run()
        check("the model isn't in the sandbox: 404 NOT_FOUND and the state is unchanged",
              envelope(client.post(f"/runs/{undeployed}/report/generate"), 404, "NOT_FOUND") and store.get(undeployed).state == RunState.MODEL_APPROVED)
        items.deployed = True

        rejected = new_run()
        items.fail = FabricOperationFailed("InvalidDefinition: bad visual", "req-1", {"code": "InvalidDefinition"})
        r = client.post(f"/runs/{rejected}/report/generate")
        check("Fabric rejects the definition: 502 UPSTREAM_ERROR, no version, no PbirReport, state unchanged",
              r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_ERROR" and "bad visual" in json.dumps(r.json())
              and not (runs_root / rejected / "report" / "v1").exists() and "PbirReport" not in store.get(rejected).artifacts
              and store.get(rejected).state == RunState.MODEL_APPROVED and not list((runs_root / rejected / "report").glob(".v*")))
        items.fail = None
        check("after the failure a retry succeeds as v1", client.post(f"/runs/{rejected}/report/generate").json()["version"] == 1)

        edited = new_run()
        path = runs_root / edited / "ReportPlan.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"Net Revenue"', '"Net Revenue X"', 1), encoding="utf-8")
        r = client.post(f"/runs/{edited}/report/generate")
        check("a hand-edited plan with a missing measure: 422 VALIDATION_ERROR naming the measure, nothing published",
              envelope(r, 422, "VALIDATION_ERROR") and "Net Revenue X" in r.json()["error"]["message"] and not (runs_root / edited / "report").exists())
        stale = new_run()
        data = json.loads((runs_root / stale / "ReportPlan.json").read_text(encoding="utf-8"))
        data["based_on"]["SemanticModel"] = "sha256:old"
        (runs_root / stale / "ReportPlan.json").write_text(json.dumps(data), encoding="utf-8")
        check("a plan made for another model version: 409 STALE_VERSION", envelope(client.post(f"/runs/{stale}/report/generate"), 409, "STALE_VERSION"))
        gap = new_run()
        data = json.loads((runs_root / gap / "ReportPlan.json").read_text(encoding="utf-8"))
        data["questions"].append({"id": "Q-R9", "text": "Extra?", "source": "contract.dimensions[X]"})
        data["unanswered_questions"] = [{"question_id": "Q-R9", "reason": "no measure"}]
        (runs_root / gap / "ReportPlan.json").write_text(json.dumps(data), encoding="utf-8")
        r = client.post(f"/runs/{gap}/report/generate")
        check("a plan with unanswered questions: 422 naming them", envelope(r, 422, "VALIDATION_ERROR") and "Q-R9" in r.json()["error"]["message"])
        os.environ.pop("SANDBOX_WORKSPACE_ID")
        os.environ.pop("FABRIC_SANDBOX_WORKSPACE_ID", None)
        nosandbox = new_run()
        check("no sandbox workspace configured: 500 CONFIG_ERROR", envelope(client.post(f"/runs/{nosandbox}/report/generate"), 500, "CONFIG_ERROR"))
        app.dependency_overrides.clear()

    print("All checks passed.")


def _edit(plan, fn):
    copy = plan.model_copy(deep=True)
    fn(copy)
    return copy


def _fails(plan, spec, tmp) -> bool:
    try:
        generate_pbir(plan, "model-1", Path(tmp) / "x", spec)
    except PbirGenerationError:
        return not (Path(tmp) / "x").exists()
    return False


if __name__ == "__main__":
    main()
