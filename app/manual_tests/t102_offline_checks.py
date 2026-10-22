"""T-102 offline checks: the API endpoints, error envelope, CORS and next_action.

Run: python -m app.manual_tests.t102_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The API runs in-process against a temporary file store, so no server, Azure or
Fabric access is needed. One logged traceback ("Unhandled error on GET /runs")
is expected: it comes from the check that an unhandled error returns a generic 500.
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.store.file_store import FileRunStore
from app.workflow.next_action import NEXT_ACTION
from app.workflow.run import ArtifactRef
from app.workflow.states import RunState


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def is_envelope(response, status: int, code: str) -> bool:
    body = response.json()
    return (
        response.status_code == status
        and set(body) == {"error"}
        and body["error"]["code"] == code
        and bool(body["error"]["message"])
    )


TEST_USER = Principal(oid="oid-1", upn="tester@contoso.com", name="Tester")


def fake_authenticate() -> AuthContext:
    """Stands in for token validation; T-105's own checks cover the real thing."""
    return AuthContext(principal=TEST_USER, token="test-token")


class BrokenStore:
    def list(self, *args, **kwargs):
        raise RuntimeError("secret internal detail")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FileRunStore(Path(tmp) / "runs")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[authenticate] = fake_authenticate
        client = TestClient(app)

        # --- Health ---
        r = client.get("/health")
        body = r.json()
        check(
            "GET /health",
            r.status_code == 200
            and body["status"] == "ok"
            and body["app"]
            and body["version"],
        )

        # --- Create ---
        r = client.post("/runs", json={"prompt": "Total sales by month"})
        run = r.json()
        check("POST /runs returns 201", r.status_code == 201)
        check(
            "RunDetail fields",
            run["state"] == "CREATED"
            and run["next_action"] == "Compile requirement"
            and run["open_questions"] == []
            and run["history"][0]["from"] is None
            and run["history"][0]["to"] == "CREATED",
        )
        run_id = run["run_id"]
        r = client.post(
            "/runs",
            json={
                "prompt": "  Net revenue  ",
                "subject_area": "Sales",
                "desired_outputs": ["report"],
                "constraints": ["Use conformed tables only"],
            },
        )
        check(
            "POST /runs with optional fields",
            r.status_code == 201
            and r.json()["prompt"] == "Net revenue"
            and r.json()["subject_area"] == "Sales"
            and r.json()["desired_outputs"] == ["report"],
        )

        # --- Read and list ---
        r = client.get(f"/runs/{run_id}")
        check("GET /runs/{id}", r.status_code == 200 and r.json()["run_id"] == run_id)
        r = client.get("/runs")
        body = r.json()
        check(
            "GET /runs summaries",
            r.status_code == 200
            and body["total"] == 2
            and len(body["items"]) == 2
            and set(body["items"][0])
            == {"run_id", "state", "prompt_preview", "created_at", "created_by"},
        )
        r = client.get("/runs", params={"limit": 1})
        check(
            "limit trims items, total stays",
            len(r.json()["items"]) == 1 and r.json()["total"] == 2,
        )
        r = client.get("/runs", params={"state": "CREATED"})
        check("state filter", r.json()["total"] == 2)
        r = client.get("/runs", params={"state": "COMPLETED"})
        check("state filter, no match", r.json() == {"items": [], "total": 0})

        # --- Artifacts ---
        r = client.get(f"/runs/{run_id}/artifacts")
        check("artifacts empty", r.status_code == 200 and r.json() == [])
        stored = store.get(run_id)
        stored.artifacts["BusinessContract"] = ArtifactRef(
            path="BusinessContract.yaml",
            version=2,
            hash="sha256:abc",
            status="approved",
            updated_at="2026-10-15T10:00:00Z",
        )
        store.save(stored)
        r = client.get(f"/runs/{run_id}/artifacts")
        check(
            "artifacts listed",
            r.json()
            == [
                {
                    "name": "BusinessContract",
                    "version": 2,
                    "path": "BusinessContract.yaml",
                    "hash": "sha256:abc",
                    "status": "approved",
                    "updated_at": "2026-10-15T10:00:00Z",
                }
            ],
        )

        # --- next_action ---
        check("next_action covers all 15 states", set(NEXT_ACTION) == set(RunState))
        for state in RunState:
            stored = store.get(run_id)
            stored.state = state
            store.save(stored)
            got = client.get(f"/runs/{run_id}").json()["next_action"]
            if got != NEXT_ACTION[state]:
                check(f"next_action for {state.value}", False)
        check("next_action matches the table for every state", True)

        # --- Errors ---
        r = client.post("/runs", json={"prompt": "   "})
        check("whitespace prompt is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        check("422 names the field", "prompt" in r.json()["error"]["message"])
        r = client.post("/runs", json={})
        check("missing prompt is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        r = client.post("/runs", json={"prompt": "a" * 10001})
        check("prompt over 10000 is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        r = client.post("/runs", json={"prompt": "a" * 10000})
        check("prompt of exactly 10000 is accepted", r.status_code == 201)
        r = client.get("/runs/00000000-0000-4000-8000-000000000000")
        check("unknown run is 404 RUN_NOT_FOUND", is_envelope(r, 404, "RUN_NOT_FOUND"))
        r = client.get("/runs/not-a-uuid/artifacts")
        check("artifacts of unknown run is 404", is_envelope(r, 404, "RUN_NOT_FOUND"))
        check("limit 201 is 422", is_envelope(client.get("/runs", params={"limit": 201}), 422, "VALIDATION_ERROR"))
        check("limit 0 is 422", is_envelope(client.get("/runs", params={"limit": 0}), 422, "VALIDATION_ERROR"))
        r = client.get("/runs", params={"state": "BOGUS"})
        check("bad state is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        check("422 lists valid states", "CREATED" in r.text and "DEPLOYING" in r.text)
        check("unknown path is 404 NOT_FOUND", is_envelope(client.get("/nope"), 404, "NOT_FOUND"))

        # --- No public transition endpoint ---
        methods = {
            (m.upper(), path)
            for path, item in app.openapi()["paths"].items()
            for m in item
        }
        check(
            "no endpoint changes state",
            not any(
                m in ("PUT", "PATCH", "DELETE")
                and not p.endswith("/scope")
                and "/contract/kpis/" not in p  # KPI edit (T-203) never changes run state
                and not p.endswith("/plan")  # plan edit (T-405) never changes run state
                for m, p in methods
            )
            and ("POST", "/runs") in methods
            and not any("transition" in p for _, p in methods),
        )

        # --- CORS ---
        r = client.get("/health", headers={"Origin": "http://localhost:3000"})
        check(
            "CORS allows localhost:3000",
            r.headers.get("access-control-allow-origin") == "http://localhost:3000",
        )
        r = client.get("/health", headers={"Origin": "http://evil.example.com"})
        check("CORS blocks other origins", "access-control-allow-origin" not in r.headers)

        # --- Unhandled exception ---
        app.dependency_overrides[get_store] = lambda: BrokenStore()
        r = TestClient(app, raise_server_exceptions=False).get("/runs")
        check(
            "unhandled error is 500 INTERNAL_ERROR",
            is_envelope(r, 500, "INTERNAL_ERROR"),
        )
        check("500 hides the details", "secret internal detail" not in r.text)

        app.dependency_overrides.clear()

        # --- Docs ---
        check("/docs is served", client.get("/docs").status_code == 200)

    print("All checks passed.")


if __name__ == "__main__":
    main()
