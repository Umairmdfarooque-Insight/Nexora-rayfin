"""Manual offline check for T-102's dispatcher (health, create, list, get,
validation, not-found, job round trip, RUN_BUSY, UDF-vs-job host check), with
the local stores (RUN_STORE=file, ARTIFACT_STORE=local) and a fake principal
standing in for a UDF invocation context / job identity.
Run: python -m app.manual_tests.t102_dispatch_offline_checks
"""

import shutil
import sys
from pathlib import Path

PRINCIPAL = {"oid": "11111111-1111-1111-1111-111111111111", "upn": "tester@contoso.com", "name": "Tester"}


def main() -> int:
    from app.fabric.dispatch import dispatch
    from app.fabric.job import read_job_result, reserve_job, run_job
    from app.store.factory import get_run_store

    root = Path("runs")
    failures = []

    def check(label, cond):
        print(f"{'OK' if cond else 'FAIL'}: {label}")
        if not cond:
            failures.append(label)

    # 1. health, no principal needed
    r = dispatch("GET", "/health", {}, None, None, as_job=False)
    check("GET /health -> 200 runtime", r["status"] == 200 and r["body"].get("app") == "Nexora")

    # 2. create
    r = dispatch("POST", "/runs", {}, {"prompt": "Total sales by month"}, PRINCIPAL, as_job=False)
    check("POST /runs -> 201", r["status"] == 201)
    run_id = r["body"]["run_id"]
    check("state CREATED, next_action Compile requirement, active_job null",
          r["body"]["state"] == "CREATED" and r["body"]["next_action"] == "Compile requirement" and r["body"]["active_job"] is None)

    # 3. read + list
    r = dispatch("GET", f"/runs/{run_id}", {}, None, PRINCIPAL, as_job=False)
    check("GET /runs/{id} -> 200", r["status"] == 200 and r["body"]["run_id"] == run_id)
    r = dispatch("GET", "/runs", {}, None, PRINCIPAL, as_job=False)
    check("GET /runs includes the new run", r["status"] == 200 and any(i["run_id"] == run_id for i in r["body"]["items"]))

    # 4. validation + not found
    r = dispatch("POST", "/runs", {}, {"prompt": ""}, PRINCIPAL, as_job=False)
    check("POST /runs with empty prompt -> 422 VALIDATION_ERROR", r["status"] == 422 and r["body"]["error"]["code"] == "VALIDATION_ERROR")
    r = dispatch("GET", "/runs/00000000-0000-0000-0000-000000000000", {}, None, PRINCIPAL, as_job=False)
    check("GET unknown run -> 404 RUN_NOT_FOUND", r["status"] == 404 and r["body"]["error"]["code"] == "RUN_NOT_FOUND")

    # 5. job-only operation called through the UDF (`call`) path -> 400 BAD_REQUEST
    r = dispatch("PUT", f"/runs/{run_id}/scope", {}, {"workspace_id": "11111111-1111-1111-1111-111111111111", "lakehouse_id": "11111111-1111-1111-1111-111111111111"}, PRINCIPAL, as_job=False)
    check("job op via call() -> 400 BAD_REQUEST", r["status"] == 400 and r["body"]["error"]["code"] == "BAD_REQUEST")

    # 6. job round trip: reserve, then run_job directly (stands in for the notebook)
    store = get_run_store()
    job_id, run = reserve_job(store, run_id, "POST /runs/{id}/compile", {})
    check("reserve_job returns a job_id and queues active_job", bool(job_id) and run.active_job.status == "queued")

    # one job at a time
    from app.fabric.errors import ApiError
    try:
        reserve_job(store, run_id, "POST /runs/{id}/compile", {})
        check("second reservation refused (RUN_BUSY)", False)
    except ApiError as exc:
        check("second reservation refused (RUN_BUSY)", exc.code == "RUN_BUSY")

    # GET /runs/{id}/jobs/{job_id} -> 404 before the job has run
    r = dispatch("GET", f"/runs/{run_id}/jobs/{job_id}", {}, None, PRINCIPAL, as_job=False)
    check("job result 404 before it exists", r["status"] == 404)

    # Run the job itself (no real Azure OpenAI configured, so the compile call
    # itself will fail -- that's fine, this only checks the job plumbing).
    result = run_job(job_id, run_id, "POST /runs/{id}/compile", {})
    check("run_job returns a result with a status", "status" in result and "body" in result)

    r = dispatch("GET", f"/runs/{run_id}/jobs/{job_id}", {}, None, PRINCIPAL, as_job=False)
    check("job result now readable", r["status"] == 200 and r["body"]["status"] == result["status"])

    r = dispatch("GET", f"/runs/{run_id}", {}, None, PRINCIPAL, as_job=False)
    check("active_job settled to succeeded/failed, not queued/running",
          r["body"]["active_job"]["status"] in ("succeeded", "failed"))
    check("next_action no longer 'Wait for …'", not r["body"]["next_action"].startswith("Wait for"))

    # 7. a second reservation is now allowed again
    job_id2, run2 = reserve_job(store, run_id, "POST /runs/{id}/compile", {})
    check("reservation allowed again once the job settled", run2.active_job.job_id == job_id2)

    shutil.rmtree(root / run_id, ignore_errors=True)
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
