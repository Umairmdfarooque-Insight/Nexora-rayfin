"""Manual offline check for T-105: principal_from_udf_context's defensive
field reading, and a job run by a different user than the one who reserved it
(FR-3: 403 FORBIDDEN, the run unchanged, nothing written).
Run: python -m app.manual_tests.t105_identity_offline_checks
"""

import shutil
import sys
from pathlib import Path
from unittest import mock

USER_A = {"oid": "aaaaaaaa-0000-0000-0000-000000000000", "upn": "alice@contoso.com", "name": "Alice"}
USER_B = {"oid": "bbbbbbbb-0000-0000-0000-000000000000", "upn": "bob@contoso.com", "name": "Bob"}


def main() -> int:
    from app.fabric.identity import principal_from_udf_context
    from app.fabric.job import reserve_job, run_job
    from app.store.factory import get_run_store

    failures = []

    def check(label, cond):
        print(f"{'OK' if cond else 'FAIL'}: {label}")
        if not cond:
            failures.append(label)

    # dict-shaped executing_user
    class DictCtx:
        executing_user = {"Oid": "oid-1", "TenantId": "tenant-1", "PreferredUsername": "user@contoso.com"}

    principal = principal_from_udf_context(DictCtx())
    check("dict-shaped context -> principal", principal == {"oid": "oid-1", "upn": "user@contoso.com", "name": "user@contoso.com"})

    # attribute-shaped executing_user, no PreferredUsername
    class Attrs:
        Oid = "oid-2"

    class AttrCtx:
        executing_user = Attrs()

    principal2 = principal_from_udf_context(AttrCtx())
    check("attribute-shaped context, no username -> upn falls back to oid", principal2 == {"oid": "oid-2", "upn": "oid-2", "name": "oid-2"})

    # no context / no executing_user -> None, never raises
    check("None context -> None", principal_from_udf_context(None) is None)

    class NoUser:
        pass

    check("context with no executing_user -> None", principal_from_udf_context(NoUser()) is None)

    # job identity mismatch: reserved by A, run as B -> 403, nothing written
    store = get_run_store()
    run = store.create("Total sales by month", created_by=USER_A)
    run_id = run.run_id

    job_id, _ = reserve_job(store, run_id, "POST /runs/{id}/compile", {}, USER_A)

    with mock.patch("app.fabric.identity.principal_from_job", return_value=USER_B):
        result = run_job(job_id, run_id, "POST /runs/{id}/compile", {})
    check("mismatched runner -> 403 FORBIDDEN", result["status"] == 403 and result["body"]["error"]["code"] == "FORBIDDEN")

    from app.fabric.job import read_job_result
    from app.workflow.clarification import NotFound

    wrote_nothing = False
    try:
        read_job_result(run_id, job_id)
    except NotFound:
        wrote_nothing = True
    check("no result file was written", wrote_nothing)

    refreshed = store.get(run_id)
    check("active_job is still queued (untouched)", refreshed.active_job is not None and refreshed.active_job.status == "queued")

    # now run it as the same user who reserved it -> proceeds (settles to
    # succeeded/failed, doesn't matter which; this has no real LLM config)
    with mock.patch("app.fabric.identity.principal_from_job", return_value=USER_A):
        result2 = run_job(job_id, run_id, "POST /runs/{id}/compile", {})
    check("matching runner -> job actually ran", result2["status"] != 403)

    shutil.rmtree(Path("runs") / run_id, ignore_errors=True)
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
