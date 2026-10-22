"""Offline checks for the API side of T-205 (Clarification and contract review UI).

Run: python -m app.manual_tests.t205_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Covers what the UI reads: `contract` and `artifacts` on the run detail, and GET /me with the
caller's roles. The UI itself is tested in the browser (see .claude/MANUAL_TESTING.md) and its
display rules by `npm test` in rayfin-ui.
"""

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.manual_tests.t203_offline_checks import FakeLlm
from app.store.file_store import FileRunStore
from app.workflow import roles as roles_module

failures: list[str] = []
UPN = "tester@contoso.com"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        roles_file = tmp_dir / "roles.yaml"
        roles_file.write_text(f"roles:\n  business_owner: [{UPN.upper()}]\n  bi_engineer: [{UPN}]\n", encoding="utf-8")
        original = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        llm = FakeLlm()
        user = Principal(oid="oid-1", upn=UPN, name="Tester")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_llm] = lambda: llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=user, token="t")
        client = TestClient(app)
        try:
            me = client.get("/me")
            check("GET /me returns the user and roles", me.status_code == 200 and me.json() == {"oid": "oid-1", "upn": UPN, "name": "Tester", "roles": ["business_owner", "bi_engineer"]}, me.text)
            roles_file.write_text("roles:\n  business_owner: [someone@contoso.com]\n", encoding="utf-8")
            check("GET /me shows no roles for a user who holds none", client.get("/me").json()["roles"] == [])
            app.dependency_overrides.pop(authenticate)
            check("GET /me needs a token", client.get("/me").status_code in (401, 500))
            app.dependency_overrides[authenticate] = lambda: AuthContext(principal=user, token="t")
            roles_file.write_text(f"roles:\n  business_owner: [{UPN}]\n", encoding="utf-8")

            rid = store.create("Show revenue by month for 2024").run_id
            fresh = client.get(f"/runs/{rid}").json()
            check("a run with no contract has contract null and no artifacts", fresh["contract"] is None and fresh["artifacts"] == {}, str(fresh.get("contract")))

            compiled = client.post(f"/runs/{rid}/compile").json()
            check("compile returns the contract inside the run detail", compiled["run"]["contract"] is not None and compiled["run"]["contract"]["version"] == compiled["contract"]["version"])
            detail = client.get(f"/runs/{rid}").json()
            artifact = detail["artifacts"]["BusinessContract"]
            check("GET /runs/{id} has the contract and the hash to send as expected_hash", detail["contract"]["status"] == "draft" and artifact["hash"].startswith("sha256:"))
            check("the contract in the run detail is the file's content", bc.parse(open(runs_root / rid / "BusinessContract.yaml", encoding="utf-8").read()).model_dump(mode="json") == detail["contract"])

            answered = client.post(f"/runs/{rid}/questions/RULE-REVENUE/answer", json={"answer": "Net Revenue = SUM(LineTotal) excluding cancelled orders"}).json()
            check("answering returns the updated contract in the run detail", answered["run"]["contract"]["locked_facts"] and answered["run"]["contract"]["version"] == answered["contract"]["version"])
            dismissed = client.post(f"/runs/{rid}/questions/Q1/dismiss", json={"reason": "Out of scope"}).json()
            check("dismissing returns the updated contract in the run detail", any(q["status"] == "dismissed" for q in dismissed["run"]["contract"]["open_questions"]))

            hash_now = client.get(f"/runs/{rid}").json()["artifacts"]["BusinessContract"]["hash"]
            accepted = client.post(f"/runs/{rid}/approvals", json={"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": hash_now}).json()
            check("accept returns the locked contract and the new hash in the run", accepted["run"]["contract"]["status"] == "approved" and accepted["run"]["artifacts"]["BusinessContract"]["status"] == "approved" and accepted["run"]["artifacts"]["BusinessContract"]["hash"] != hash_now)
            check("the GET after approval agrees", client.get(f"/runs/{rid}").json()["contract"]["status"] == "approved")
        finally:
            roles_module.CONFIG_PATH = original
            app.dependency_overrides.clear()

    if failures:
        print(f"\n{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
