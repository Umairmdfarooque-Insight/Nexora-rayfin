"""Offline checks for T-204 (Approval service and Gate G1).

Run: python -m app.manual_tests.t204_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The LLM is faked, roles come from a temporary roles file, and the API runs in-process
against a temporary run folder, so no Azure or Fabric access is needed.
"""

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler.critical_terms import ConfigError
from app.contracts import business_contract as bc
from app.manual_tests.t203_offline_checks import FakeLlm
from app.store.file_store import FileRunStore
from app.workflow import roles as roles_module
from app.workflow.gates import run_g1
from app.workflow.states import RunState

failures: list[str] = []
UPN = "tester@contoso.com"
DEFINITION = "Net Revenue = SUM(LineTotal) excluding cancelled orders"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


class RecordingLlm(FakeLlm):
    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def __call__(self, system, user, schema):
        self.prompts.append(user)
        return super().__call__(system, user, schema)


def code(response) -> str:
    return response.json().get("error", {}).get("code", "")


def write_roles(path: Path, owners: list[str]) -> None:
    body = "roles:\n  business_owner: [" + ", ".join(owners) + "]\n  bi_engineer: [other@contoso.com]\n"
    path.write_text(body, encoding="utf-8")


def main() -> int:
    # ---- roles.yaml ----
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roles.yaml"
        try:
            roles_module.load_roles(path)
            named = False
        except ConfigError as exc:
            named = "roles.yaml" in str(exc)
        check("missing roles.yaml raises ConfigError naming the file", named)
        for label, body in (("unknown role", "roles:\n  wizard: [a@b.c]\n"), ("not a mapping", "- a\n"), ("bad list", "roles:\n  business_owner: nope\n")):
            path.write_text(body, encoding="utf-8")
            try:
                roles_module.load_roles(path)
                raised = False
            except ConfigError as exc:
                raised = "roles.yaml" in str(exc)
            check(f"invalid roles.yaml ({label}) raises ConfigError", raised)
        write_roles(path, ["Tester@Contoso.com"])
        loaded = roles_module.load_roles(path)
        check("UPN matching ignores case", loaded.has("TESTER@contoso.com", "business_owner") and not loaded.has("x@y.z", "business_owner"))
    shipped = roles_module.load_roles()
    check("the shipped config/roles.yaml loads and lists business_owner", "business_owner" in shipped.members)

    # ---- G1 unit checks ----
    sample = bc.load(Path(__file__).resolve().parents[2] / "samples" / "contract_valid.yaml")
    ok = run_g1(sample, "sha256:x")
    by_id = {c.id: c.status for c in ok.checks}
    check("G1 passes the valid sample, with G1.5 a warning (not yet assessed)", ok.passed and by_id["G1.5"] == "warning" and by_id["G1.1"] == "pass", str(by_id))
    gap = sample.model_copy(update={"kpis": [sample.kpis[0].model_copy(update={"status": bc.KpiStatus.DATA_GAP})]})
    check("G1.4 fails on a data_gap KPI", {c.id: c.status for c in run_g1(gap, "h").checks}["G1.4"] == "fail")
    bare = sample.model_copy(update={"kpis": [sample.kpis[0].model_copy(update={"grain": None})]})
    g13 = next(c for c in run_g1(bare, "h").checks if c.id == "G1.3")
    check("G1.3 fails when a KPI lacks grain, naming it", g13.status == "fail" and "grain" in str(g13.evidence))
    none = sample.model_copy(update={"kpis": []})
    check("G1.3 fails when there is no KPI", next(c for c in run_g1(none, "h").checks if c.id == "G1.3").status == "fail")
    partial = sample.model_copy(update={"feasibility": [bc.Feasibility(kpi="Other", status=bc.FeasibilityStatus.FEASIBLE)]})
    check("G1.5 fails when feasibility exists but misses a KPI", next(c for c in run_g1(partial, "h").checks if c.id == "G1.5").status == "fail")

    # ---- API ----
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        roles_file = tmp_dir / "roles.yaml"
        write_roles(roles_file, [UPN])
        original_roles = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        llm = RecordingLlm()
        user = Principal(oid="oid-1", upn=UPN, name="Tester")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_llm] = lambda: llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=user, token="t")
        client = TestClient(app)

        def new_run() -> str:
            rid = store.create("Show revenue by month for 2024").run_id
            client.post(f"/runs/{rid}/compile")
            return rid

        def answer(rid: str) -> None:
            client.post(f"/runs/{rid}/questions/RULE-REVENUE/answer", json={"answer": DEFINITION})

        def h(rid: str) -> str:
            return store.get(rid).artifacts["BusinessContract"].hash

        def act(rid: str, action: str, **extra):
            return client.post(f"/runs/{rid}/approvals", json={"artifact": "BusinessContract", "action": action, "expected_hash": h(rid), **extra})

        def records(rid: str) -> list[dict]:
            p = runs_root / rid / "approvals.jsonl"
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []

        try:
            # AC1: gate blocks
            r1 = new_run()
            blocked = act(r1, "ACCEPT")
            failed = blocked.json().get("error", {}).get("details", {}).get("failed_checks", [])
            check("AC1 accept with a blocking question is 409 GATE_FAILED", blocked.status_code == 409 and code(blocked) == "GATE_FAILED", blocked.text[:200])
            check("AC1 G1.2 is a failing check (G1.3 too: the KPI has no definition)", "G1.2" in failed and "G1.3" in failed, str(failed))
            check("AC1 nothing is locked or recorded", store.get(r1).state is RunState.NEEDS_CLARIFICATION and not records(r1) and bc.load(runs_root / r1 / "BusinessContract.yaml").status is bc.ContractStatus.DRAFT)
            check("AC1 the failed gate result is saved under gates/", len(list((runs_root / r1 / "gates").glob("G1-*.json"))) == 1)

            # G1 check endpoint
            checked = client.post(f"/runs/{r1}/gates/G1/check")
            body = checked.json()
            check("G1 check returns a GateResult", checked.status_code == 200 and body["gate"] == "G1" and body["passed"] is False and {c["id"] for c in body["checks"]} == {"G1.1", "G1.2", "G1.3", "G1.4", "G1.5"}, checked.text[:200])
            check("G1 check records the contract hash and saves a file", body["artifact_hash"] == h(r1) and len(list((runs_root / r1 / "gates").glob("G1-*.json"))) == 2)
            check("unknown gate is 404 NOT_FOUND", code(client.post(f"/runs/{r1}/gates/G9/check")) == "NOT_FOUND")
            check("G1 check before any compile is 404 NOT_FOUND", code(client.post(f"/runs/{store.create('x').run_id}/gates/G1/check")) == "NOT_FOUND")

            # AC2: approve
            answer(r1)
            check("G1 passes after the answer", client.post(f"/runs/{r1}/gates/G1/check").json()["passed"] is True)
            stale = client.post(f"/runs/{r1}/approvals", json={"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": "sha256:old"})
            check("accept with an old expected_hash is 409 STALE_VERSION", stale.status_code == 409 and code(stale) == "STALE_VERSION")
            accepted = act(r1, "ACCEPT", comment="Looks right")
            ab = accepted.json()
            check("AC2 accept returns 200 with approval, run and gate_result", accepted.status_code == 200 and {"approval", "run", "gate_result"} <= set(ab), accepted.text[:300])
            check("AC2 state is REQUIREMENT_APPROVED", ab["run"]["state"] == "REQUIREMENT_APPROVED" and store.get(r1).state is RunState.REQUIREMENT_APPROVED)
            locked = bc.load(runs_root / r1 / "BusinessContract.yaml")
            check("AC2 the contract is approved", locked.status is bc.ContractStatus.APPROVED)
            recs = records(r1)
            artifact = store.get(r1).artifacts["BusinessContract"]
            check("AC2 approvals.jsonl has one record with UPN, timestamp and hash", len(recs) == 1 and recs[0]["user"]["upn"] == UPN and recs[0]["at"] and recs[0]["artifact_hash"] == artifact.hash and recs[0]["artifact_version"] == artifact.version and recs[0]["action"] == "ACCEPT" and recs[0]["role"] == "business_owner", str(recs))
            check("the artifact is marked approved and its hash is the file's hash", artifact.status == "approved" and artifact.hash == bc.content_hash((runs_root / r1 / "BusinessContract.yaml").read_bytes()))
            check("the record points at the saved gate result", recs[0]["gate_result_ref"].startswith("gates/G1-") and (runs_root / r1 / recs[0]["gate_result_ref"]).is_file())
            listed = client.get(f"/runs/{r1}/approvals")
            check("GET approvals lists the record", listed.status_code == 200 and [r["approval_id"] for r in listed.json()] == [recs[0]["approval_id"]])
            check("GET approvals for a run with none is []", client.get(f"/runs/{store.create('y').run_id}/approvals").json() == [])
            check("GET approvals for an unknown run is 404", client.get("/runs/nope/approvals").status_code == 404)
            check("the run's history shows the approver", store.get(r1).history[-1].actor == UPN and store.get(r1).history[-1].to is RunState.REQUIREMENT_APPROVED)

            # approved contract is locked
            again = act(r1, "ACCEPT")
            check("accept on an approved contract is 409 ALREADY_APPROVED", again.status_code == 409 and code(again) == "ALREADY_APPROVED", again.text[:200])
            name = locked.kpis[0].name.replace(" ", "%20")
            put = client.put(f"/runs/{r1}/contract/kpis/{name}", json={"currency": "EUR", "expected_hash": h(r1)})
            check("AC3 KPI edit after approval is 409 CONTRACT_LOCKED", put.status_code == 409 and code(put) == "CONTRACT_LOCKED", put.text[:200])
            check("edit action after approval is 409 CONTRACT_LOCKED", code(act(r1, "EDIT", edits={"kpis": {locked.kpis[0].name: {"currency": "EUR"}}})) == "CONTRACT_LOCKED")
            check("reject after approval is 409 CONTRACT_LOCKED", code(act(r1, "REJECT", comment="no")) == "CONTRACT_LOCKED")
            check("regenerate after approval is 409 CONTRACT_LOCKED", code(act(r1, "REGENERATE")) == "CONTRACT_LOCKED")
            check("no extra records were written by the refused actions", len(records(r1)) == 1)
            explained_locked = act(r1, "EXPLAIN")
            check("explain still works on an approved contract", explained_locked.status_code == 200 and len(records(r1)) == 2)
            check("records are only appended, so the first line is unchanged", records(r1)[0]["approval_id"] == recs[0]["approval_id"])

            # AC4: role enforced
            r2 = new_run()
            answer(r2)
            write_roles(roles_file, ["someone.else@contoso.com"])
            denied = act(r2, "ACCEPT")
            check("AC4 accept without business_owner is 403 FORBIDDEN", denied.status_code == 403 and code(denied) == "FORBIDDEN", denied.text[:200])
            check("AC4 reject without business_owner is 403 FORBIDDEN", code(act(r2, "REJECT", comment="x")) == "FORBIDDEN")
            check("403 leaves the run untouched", store.get(r2).state is RunState.CREATED and not records(r2))
            check("any authenticated user can explain", act(r2, "EXPLAIN").status_code == 200)
            check("role is null in the record of a user with no role", records(r2)[-1]["role"] is None)
            write_roles(roles_file, [UPN])
            check("restoring the role takes effect without a restart", act(r2, "EXPLAIN").status_code == 200 and records(r2)[-1]["role"] == "business_owner")

            # AC6: explain
            ex = act(r2, "EXPLAIN").json()
            check("AC6 explanation has a rationale per KPI and question", ex["explanation"]["kpis"] and all(k["rationale"] for k in ex["explanation"]["kpis"]) and ex["explanation"]["questions"] and all(q["rationale"] for q in ex["explanation"]["questions"]), str(ex["explanation"]))
            check("AC6 the KPI rationale states its definition", "SUM(LineTotal)" in ex["explanation"]["kpis"][0]["rationale"])
            check("AC6 an EXPLAIN record exists and the state is unchanged", records(r2)[-1]["action"] == "EXPLAIN" and store.get(r2).state is RunState.CREATED)

            # EDIT
            kpi_name = bc.load(runs_root / r2 / "BusinessContract.yaml").kpis[0].name
            before_hash = h(r2)
            edit = act(r2, "EDIT", comment="currency", edits={"kpis": {kpi_name.lower(): {"currency": "EUR"}}})
            check("EDIT changes the KPI and writes a record", edit.status_code == 200 and bc.load(runs_root / r2 / "BusinessContract.yaml").kpis[0].currency == "EUR" and records(r2)[-1]["action"] == "EDIT", edit.text[:200])
            check("EDIT records the version it produced", records(r2)[-1]["artifact_hash"] == h(r2) != before_hash)
            check("EDIT without edits is 422", act(r2, "EDIT").status_code == 422)
            check("EDIT of an unknown KPI is 404", act(r2, "EDIT", edits={"kpis": {"Nope": {"currency": "EUR"}}}).status_code == 404)
            check("EDIT with an unknown field is 422", act(r2, "EDIT", edits={"kpis": {kpi_name: {"status": "approved"}}}).status_code == 422)

            # AC5: reject
            no_comment = act(r2, "REJECT")
            check("reject without a comment is 422", no_comment.status_code == 422, no_comment.text[:150])
            check("reject with a blank comment is 422", act(r2, "REJECT", comment="   ").status_code == 422)
            rejected = act(r2, "REJECT", comment="Exclude tax as well")
            check("AC5 reject returns 200 and the state is NEEDS_CLARIFICATION", rejected.status_code == 200 and rejected.json()["run"]["state"] == "NEEDS_CLARIFICATION", rejected.text[:200])
            rev = next((q for q in rejected.json()["run"]["open_questions"] if q["source"] == "reviewer"), None)
            check("AC5 a blocking reviewer question holds the comment", rev is not None and rev["blocking"] and rev["text"] == "Exclude tax as well" and rev["id"] == "REV-1", str(rev))
            check("AC5 a REJECT record with the comment exists", records(r2)[-1]["action"] == "REJECT" and records(r2)[-1]["comment"] == "Exclude tax as well")
            check("a rejected contract fails G1.2", client.post(f"/runs/{r2}/gates/G1/check").json()["checks"][1]["status"] == "fail")
            recompiled = client.post(f"/runs/{r2}/compile").json()
            check("a recompile keeps the open reviewer question", any(q["id"] == "REV-1" and q["status"] == "open" for q in recompiled["contract"]["open_questions"]) and recompiled["run"]["state"] == "NEEDS_CLARIFICATION")
            resolved = client.post(f"/runs/{r2}/questions/REV-1/answer", json={"answer": "Exclude tax from Net Revenue"})
            check("answering the reviewer question returns the run to CREATED", resolved.status_code == 200 and resolved.json()["run"]["state"] == "CREATED", resolved.text[:200])
            second = act(r2, "REJECT", comment="Also exclude freight")
            check("a second rejection gets the next id REV-2", any(q["id"] == "REV-2" for q in second.json()["run"]["open_questions"]))

            # REGENERATE keeps locked facts and passes the comment as guidance
            r3 = new_run()
            answer(r3)
            facts_before = bc.load(runs_root / r3 / "BusinessContract.yaml").locked_facts
            calls = len(llm.prompts)
            regen = act(r3, "REGENERATE", comment="Use fiscal months")
            check("REGENERATE returns 200 and records the action", regen.status_code == 200 and records(r3)[-1]["action"] == "REGENERATE", regen.text[:200])
            check("REGENERATE sends the comment to the model as guidance", len(llm.prompts) == calls + 1 and "Use fiscal months" in llm.prompts[-1])
            after = bc.load(runs_root / r3 / "BusinessContract.yaml")
            check("REGENERATE keeps the locked facts", after.locked_facts == facts_before)
            check("REGENERATE writes a new contract version", after.version > 2)

            # request validation
            check("unknown artifact is 422", client.post(f"/runs/{r3}/approvals", json={"artifact": "ReportPlan", "action": "ACCEPT", "expected_hash": h(r3)}).status_code == 422)
            check("unknown action is 422", client.post(f"/runs/{r3}/approvals", json={"artifact": "BusinessContract", "action": "NUKE", "expected_hash": h(r3)}).status_code == 422)
            check("missing expected_hash is 422", client.post(f"/runs/{r3}/approvals", json={"artifact": "BusinessContract", "action": "EXPLAIN"}).status_code == 422)
            check("approvals before any compile are 404 NOT_FOUND", code(client.post(f"/runs/{store.create('z').run_id}/approvals", json={"artifact": "BusinessContract", "action": "EXPLAIN", "expected_hash": "x"})) == "NOT_FOUND")

            # accept in the wrong state
            r4 = new_run()
            answer(r4)
            store.transition(r4, RunState.REQUIREMENT_APPROVED, "test", "test")
            check("accept in REQUIREMENT_APPROVED with a draft contract is 409", act(r4, "ACCEPT").status_code == 409)

            # missing roles.yaml is a CONFIG_ERROR
            roles_file.unlink()
            r5 = new_run()
            answer(r5)
            missing = act(r5, "ACCEPT")
            check("a missing roles.yaml is 500 CONFIG_ERROR naming the file", missing.status_code == 500 and code(missing) == "CONFIG_ERROR" and "roles.yaml" in missing.json()["error"]["message"], missing.text[:200])
        finally:
            roles_module.CONFIG_PATH = original_roles
            app.dependency_overrides.clear()

    if failures:
        print(f"\n{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
