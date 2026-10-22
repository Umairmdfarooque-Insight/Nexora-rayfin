"""Offline checks for T-203 (Clarification loop).

Run: python -m app.manual_tests.t203_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The LLM is replaced by a fake and the API runs in-process against a temporary run folder,
so no Azure or Fabric access is needed.
"""

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.manual_tests.t202_offline_checks import draft_for
from app.store.file_store import FileRunStore
from app.workflow.states import RunState

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


class FakeLlm:
    """Returns the T-202 draft, plus one non-blocking question. `mode` changes the output:
    "contradict" gives the Revenue KPI a different definition."""

    def __init__(self):
        self.mode = "normal"
        self.calls = 0

    def __call__(self, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        draft = draft_for(user)
        draft["open_questions"] = [
            {"text": "Should the report include returned orders?", "blocking": False, "reason": "Detail"}
        ]
        if self.mode == "contradict":
            draft["kpis"][0]["formula_intent"] = "SUM of TotalDue"
        return json.dumps(draft)


def error_code(response) -> str:
    return response.json().get("error", {}).get("code", "")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        llm = FakeLlm()
        user = Principal(oid="oid-1", upn="tester@contoso.com", name="Tester")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_llm] = lambda: llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=user, token="t")
        client = TestClient(app)
        try:
            run = store.create("Show revenue by month for 2024")
            rid = run.run_id
            first = client.post(f"/runs/{rid}/compile").json()
            ids = {q["id"] for q in first["run"]["open_questions"]}
            check("compile leaves RULE-REVENUE and Q1 open", ids == {"RULE-REVENUE", "Q1"}, str(ids))

            # AC1: answer closes the question
            text = "Net Revenue = SUM(LineTotal) excluding cancelled orders"
            response = client.post(f"/runs/{rid}/questions/RULE-REVENUE/answer", json={"answer": text})
            body = response.json()
            check("AC1 answer returns 200 with run and contract", response.status_code == 200 and set(body) == {"run", "contract"}, response.text[:200])
            contract = bc.BusinessContract.model_validate(body["contract"])
            question = next(q for q in contract.open_questions if q.id == "RULE-REVENUE")
            check("AC1 question is answered and keeps the answer", question.status is bc.QuestionStatus.ANSWERED and question.answer == text)
            fact = next((f for f in contract.locked_facts if f.source_question_id == "RULE-REVENUE"), None)
            check("AC1 a locked fact names the question", fact is not None and fact.text == text and fact.locked_at)
            check("AC1 the KPI formula_intent is the answer", contract.kpis[0].formula_intent == text, str(contract.kpis[0].formula_intent))

            # AC2: state advances
            detail = client.get(f"/runs/{rid}").json()
            check("AC2 state is CREATED", detail["state"] == "CREATED", detail["state"])
            check("AC2 next action is Compile requirement", detail["next_action"] == "Compile requirement")
            check("AC2 only the non-blocking question is still open", [q["id"] for q in detail["open_questions"]] == ["Q1"])
            history = store.get(rid).history
            check("AC2 history ends NEEDS_CLARIFICATION -> CREATED by the user", history[-1].to is RunState.CREATED and history[-1].actor == "tester@contoso.com")

            # AC3: locked fact survives a compile
            before = contract.locked_facts
            again = bc.BusinessContract.model_validate(client.post(f"/runs/{rid}/compile").json()["contract"])
            check("AC3 the locked fact is unchanged", again.locked_facts == before)
            reopened = [q for q in again.open_questions if q.id == "RULE-REVENUE" and q.status is bc.QuestionStatus.OPEN]
            check("AC3 the answered question is not asked again", not reopened)
            check("AC3 the definition is still on the KPI", again.kpis[0].formula_intent == text)

            # contradiction is re-applied and logged
            llm.mode = "contradict"
            fixed = bc.BusinessContract.model_validate(client.post(f"/runs/{rid}/compile").json()["contract"])
            check("contradicting LLM output is overridden by the locked fact", fixed.kpis[0].formula_intent == text)
            logs = list((runs_root / rid / "logs").glob("compile-*.json"))
            logged = [w for p in logs for w in json.loads(p.read_text(encoding="utf-8")).get("warnings", [])]
            check("the contradiction is recorded in the compile log", any("LF-RULE-REVENUE" in w for w in logged), str(logged))
            llm.mode = "normal"

            # AC4: dismiss rules
            version_before = store.get(rid).artifacts["BusinessContract"].version
            blocked = client.post(f"/runs/{rid}/questions/Q1/dismiss", json={"reason": ""})
            check("empty dismiss reason is 422", blocked.status_code == 422, blocked.text[:120])
            ok = client.post(f"/runs/{rid}/questions/Q1/dismiss", json={"reason": "Out of scope"})
            check("AC4 dismissing a non-blocking question is 200", ok.status_code == 200, ok.text[:200])
            dismissed = next(q for q in ok.json()["contract"]["open_questions"] if q["id"] == "Q1")
            check("AC4 dismissed status and reason are stored", dismissed["status"] == "dismissed" and dismissed["dismiss_reason"] == "Out of scope")
            check("dismissing writes a new version without calling the LLM", store.get(rid).artifacts["BusinessContract"].version == version_before + 1)
            check("a dismissed question isn't listed as open", client.get(f"/runs/{rid}").json()["open_questions"] == [])
            twice = client.post(f"/runs/{rid}/questions/Q1/dismiss", json={"reason": "again"})
            check("dismissing a closed question is 409 QUESTION_CONFLICT", twice.status_code == 409 and error_code(twice) == "QUESTION_CONFLICT")
            recompiled = bc.BusinessContract.model_validate(client.post(f"/runs/{rid}/compile").json()["contract"])
            check("a dismissed question stays dismissed after a recompile", [q.status.value for q in recompiled.open_questions if q.id == "Q1"] == ["dismissed"] and len([q for q in recompiled.open_questions if "returned orders" in q.text]) == 1)

            # blocking question can't be dismissed
            other = store.create("Show revenue by month for 2024")
            client.post(f"/runs/{other.run_id}/compile")
            refused = client.post(f"/runs/{other.run_id}/questions/RULE-REVENUE/dismiss", json={"reason": "skip"})
            check("AC4 dismissing a blocking question is 409", refused.status_code == 409 and "must be answered" in refused.json()["error"]["message"], refused.text[:200])
            check("the blocking question is still open", store.get(other.run_id).state is RunState.NEEDS_CLARIFICATION)

            # edge cases: not found, empty answer, re-answer
            check("unknown question is 404 NOT_FOUND", client.post(f"/runs/{rid}/questions/Q99/answer", json={"answer": "x"}).status_code == 404)
            check("answer on an unknown run is 404 RUN_NOT_FOUND", error_code(client.post("/runs/nope/questions/Q1/answer", json={"answer": "x"})) == "RUN_NOT_FOUND")
            check("empty answer is 422", client.post(f"/runs/{rid}/questions/Q1/answer", json={"answer": "   "}).status_code == 422)
            check("answer over 2000 characters is 422", client.post(f"/runs/{rid}/questions/Q1/answer", json={"answer": "x" * 2001}).status_code == 422)
            newer = "Net Revenue = SUM(LineTotal - Discount), in USD"
            redo = client.post(f"/runs/{rid}/questions/RULE-REVENUE/answer", json={"answer": newer})
            redo_contract = bc.BusinessContract.model_validate(redo.json()["contract"])
            facts = [f for f in redo_contract.locked_facts if f.source_question_id == "RULE-REVENUE"]
            check("re-answering replaces the locked fact", redo.status_code == 200 and len(facts) == 1 and facts[0].text == newer)
            old = bc.load(runs_root / rid / "contracts" / "BusinessContract.v1.yaml")
            check("the old versions keep the earlier answer", old.open_questions[0].answer is None)
            history_text = "".join(p.read_text(encoding="utf-8") for p in (runs_root / rid / "contracts").glob("*.yaml"))
            check("the earlier answer stays in contract history", text in history_text)

            # AC5: concurrent edit
            current_hash = store.get(rid).artifacts["BusinessContract"].hash
            name = redo_contract.kpis[0].name.lower()  # the fake LLM names it Net Revenue once the answer says so
            stale = client.put(
                f"/runs/{rid}/contract/kpis/{name.replace(' ', '%20')}",
                json={"formula_intent": "x", "expected_hash": "sha256:old"},
            )
            check("AC5 an old expected_hash is 409 STALE_VERSION", stale.status_code == 409 and error_code(stale) == "STALE_VERSION", stale.text[:200])
            edit = client.put(
                f"/runs/{rid}/contract/kpis/{name.replace(' ', '%20')}",
                json={"formula_intent": "SUM(LineTotal) net of returns", "currency": "EUR", "expected_hash": current_hash},
            )
            check("a KPI edit with the current hash is 200 (case-insensitive name)", edit.status_code == 200 and set(edit.json()) == {"contract"}, edit.text[:200])
            kpi = edit.json()["contract"]["kpis"][0]
            check("only the sent fields change", kpi["currency"] == "EUR" and kpi["formula_intent"] == "SUM(LineTotal) net of returns" and kpi["time_logic"] == "calendar month")
            check("the edit is a new version and a new hash", store.get(rid).artifacts["BusinessContract"].hash != current_hash)
            check("the edit keeps the run state", store.get(rid).state is RunState.CREATED)
            kept = bc.BusinessContract.model_validate(client.post(f"/runs/{rid}/compile").json()["contract"])
            check("a recompile keeps the owner's edit of a locked definition", kept.kpis[0].formula_intent == "SUM(LineTotal) net of returns", str(kept.kpis[0].formula_intent))
            check("the same hash twice: the second is stale", client.put(f"/runs/{rid}/contract/kpis/{name.title().replace(' ', '%20')}", json={"currency": "USD", "expected_hash": current_hash}).status_code == 409)
            new_hash = store.get(rid).artifacts["BusinessContract"].hash
            check("unknown KPI is 404 NOT_FOUND", client.put(f"/runs/{rid}/contract/kpis/Nope", json={"currency": "USD", "expected_hash": new_hash}).status_code == 404)
            check("unknown field in the edit is 422", client.put(f"/runs/{rid}/contract/kpis/{name.title().replace(' ', '%20')}", json={"status": "approved", "expected_hash": new_hash}).status_code == 422)
            check("missing expected_hash is 422", client.put(f"/runs/{rid}/contract/kpis/{name.title().replace(' ', '%20')}", json={"currency": "USD"}).status_code == 422)

            # approved contract is locked
            path = runs_root / rid / "BusinessContract.yaml"
            approved = bc.load(path).model_copy(update={"status": bc.ContractStatus.APPROVED})
            bc.save(approved, path)
            locked = client.put(f"/runs/{rid}/contract/kpis/{name.title().replace(' ', '%20')}", json={"currency": "USD", "expected_hash": new_hash})
            check("edit after approval is 409 CONTRACT_LOCKED", locked.status_code == 409 and error_code(locked) == "CONTRACT_LOCKED", locked.text[:200])
            check("answer after approval is 409 CONTRACT_LOCKED", error_code(client.post(f"/runs/{rid}/questions/Q1/answer", json={"answer": "x"})) == "CONTRACT_LOCKED")

            # wrong state with a draft contract
            third = store.create("Show revenue by month for 2024")
            client.post(f"/runs/{third.run_id}/compile")
            client.post(f"/runs/{third.run_id}/questions/RULE-REVENUE/answer", json={"answer": text})
            store.transition(third.run_id, RunState.REQUIREMENT_APPROVED, "test", "test")
            wrong = client.post(f"/runs/{third.run_id}/questions/Q1/answer", json={"answer": "x"})
            check("answer in REQUIREMENT_APPROVED is 409 WRONG_STATE", wrong.status_code == 409 and error_code(wrong) == "WRONG_STATE", wrong.text[:200])

            # no contract yet
            fresh = store.create("Show revenue by month for 2024")
            check("answer before any compile is 404 NOT_FOUND", error_code(client.post(f"/runs/{fresh.run_id}/questions/Q1/answer", json={"answer": "x"})) == "NOT_FOUND")
        finally:
            app.dependency_overrides.clear()

    # model rule: a blocking question can't be dismissed
    data = bc.load(Path(__file__).resolve().parents[2] / "samples" / "contract_valid.yaml").model_dump(mode="json")
    data["open_questions"] = [{"id": "Q1", "text": "t", "blocking": True, "status": "dismissed", "dismiss_reason": "r"}]
    _, issues = bc.check(data)
    check("the contract rejects a dismissed blocking question", any("can't be dismissed" in i for i in issues), str(issues))

    if failures:
        print(f"\n{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
