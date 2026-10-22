"""Job reservation and execution (T-102 FR-3, FR-7; CONTRACTS.md#fabric-operations).

`reserve_job` is what `POST /runs/{id}/jobs` calls: it takes `active_job`
(`status: queued`) so only one job runs per run at a time. The UI then starts
`nexora_stage_runner` through the Fabric Job Scheduler API with `job_id`,
`run_id`, `operation` and `payload` as parameters; the notebook's parameter
cell calls `run_job(job_id, run_id, operation, payload)`, which this module
implements. `reconcile_active_job` is the "whenever the run is read" check
(FR-7): a reservation that never got a `job_instance_id` is released after 10
minutes, and a job that ended without a result file is marked `failed`.
"""

import json
import logging
import time
import uuid
from typing import Any

from app.fabric.errors import ApiError, map_exception
from app.observability.context import bind as bind_context
from app.store.artifacts import NotFound as ArtifactNotFound
from app.store.base import RunStore
from app.store.factory import get_artifact_store
from app.workflow.clarification import NotFound
from app.workflow.run import ActiveJob, Run, utc_now

logger = logging.getLogger(__name__)

RESERVATION_TIMEOUT_SECONDS = 600  # FR-7: no job_instance_id within 10 minutes -> cancelled
LEASE_OWNER_PREFIX = "job:"
JOB_LEASE_SECONDS = 1800  # long enough for the slowest stage (report generate/validate)


def reserve_job(
    store: RunStore, run_id: str, operation: str, body: dict, principal: dict | None = None
) -> tuple[str, Run]:
    """Reserve `active_job` (status `queued`). Raises `ApiError(409, RUN_BUSY, …)`
    if a job is already active. The operation's own precondition errors (state,
    missing artifacts) surface once the job actually runs it, as the job's own
    `failed` outcome -- reserving doesn't invoke the operation itself.
    `principal` (the UDF caller who reserved it) is compared against the job's
    own identity when it runs (T-105 FR-3)."""
    from app.fabric.errors import ApiError

    run = reconcile_active_job(store, store.get(run_id))
    if run.active_job is not None and run.active_job.status in ("queued", "running"):
        raise ApiError(
            409,
            "RUN_BUSY",
            "Another operation is running on this run",
            {"active_job": run.active_job.model_dump(mode="json")},
        )
    job_id = str(uuid.uuid4())
    reserved_by = {"oid": principal.get("oid"), "upn": principal.get("upn")} if principal else None
    run.active_job = ActiveJob(
        job_id=job_id, operation=operation, status="queued", started_at=utc_now(), reserved_by=reserved_by
    )
    run.updated_at = utc_now()
    store.save(run)
    return job_id, run


def _split_operation(operation: str, run_id: str) -> tuple[str, str]:
    """`"POST /runs/{id}/compile"` -> `("POST", "/runs/<run_id>/compile")`. `{id}`
    is the one magic placeholder (the run id the job was reserved under); any
    other path parameter (a bridge id, a question id, …) must already be a
    concrete value in the `operation` string, since the caller knows it."""
    method, _, template = operation.strip().partition(" ")
    return method.upper(), template.replace("{id}", run_id)


def _write_result(run_id: str, job_id: str, result: dict) -> None:
    payload = {"status": result["status"], "body": result["body"]}
    get_artifact_store().write_atomic(
        run_id, f"jobs/{job_id}.json", json.dumps(payload, indent=2).encode("utf-8")
    )


def _update_active_job(
    store: RunStore,
    run_id: str,
    job_id: str,
    *,
    status: str,
    job_instance_id: str | None = None,
    error: dict | None = None,
) -> None:
    run = store.get(run_id)
    if run.active_job is None or run.active_job.job_id != job_id:
        return  # a newer reservation replaced this one; nothing to update
    run.active_job.status = status
    if job_instance_id is not None:
        run.active_job.job_instance_id = job_instance_id
    if status in ("succeeded", "failed", "cancelled"):
        run.active_job.finished_at = utc_now()
    if error is not None:
        run.active_job.error = error
    run.updated_at = utc_now()
    store.save(run)


def run_job(job_id: str, run_id: str, operation: str, payload: dict) -> dict:
    """Called from `nexora_stage_runner`'s parameter cell. Never raises: any
    unexpected failure is written into the result file as a mapped error too,
    so there is always a result for `GET /runs/{id}/jobs/{job_id}` to read."""
    from app.fabric.dispatch import dispatch
    from app.fabric.identity import principal_from_job
    from app.store.factory import get_run_store

    store = get_run_store()

    # T-105 FR-3: a job runs as the user who started it; if that isn't the user
    # who reserved it, refuse and write nothing (the reservation is left alone
    # and expires through the normal 10-minute timeout, FR-7).
    run = store.get(run_id)
    reserved_by = run.active_job.reserved_by if run.active_job and run.active_job.job_id == job_id else None
    runner = principal_from_job()
    if reserved_by is not None:
        matches = runner is not None and (
            runner.get("oid") == reserved_by.get("oid")
            or str(runner.get("upn") or "").casefold() == str(reserved_by.get("upn") or "").casefold()
        )
        if not matches:
            logger.warning(
                "Job %s on run %s was reserved by %s but is running as %s; refusing",
                job_id,
                run_id,
                reserved_by,
                runner,
            )
            return map_exception(
                ApiError(
                    403,
                    "FORBIDDEN",
                    "This job is running as a different user than the one who reserved it",
                )
            )

    lease_owner = f"{LEASE_OWNER_PREFIX}{job_id}"
    job_instance_id = _current_job_instance_id()
    try:
        _update_active_job(store, run_id, job_id, status="running", job_instance_id=job_instance_id)
        store.lease(run_id, lease_owner, seconds=JOB_LEASE_SECONDS)
        try:
            method, path = _split_operation(operation, run_id)
            with bind_context(job_id=job_id):  # T-704: dispatch() binds run_id/stage; this adds job_id
                result = dispatch(method, path, {}, payload or {}, runner, as_job=True)
        finally:
            store.release(run_id, lease_owner)
    except Exception as exc:  # noqa: BLE001  (the job must always leave a result)
        logger.exception("Job %s (%s) on run %s failed", job_id, operation, run_id)
        result = map_exception(exc)

    _write_result(run_id, job_id, result)
    status = "succeeded" if result["status"] < 400 else "failed"
    _update_active_job(
        store, run_id, job_id, status=status, error=result["body"] if status == "failed" else None
    )
    return result


def _current_job_instance_id() -> str | None:
    """The Job Scheduler's own id for this run, read from the notebook runtime
    context. Not yet confirmed against a live tenant which property carries it
    (fabric-hosting.md); `None` outside a job, which just means AC-7's
    "ended without a result file" check can't identify this job by instance id."""
    try:
        import notebookutils  # noqa: PLC0415

        context = notebookutils.runtime.context
        return context.get("currentJobInstanceId") or context.get("jobInstanceId")
    except Exception:
        return None


def read_job_result(run_id: str, job_id: str) -> dict:
    """The job's result file (`{"status", "body"}`, exactly what the operation
    itself returned). Raises `NotFound` (404) until it exists."""
    try:
        data = get_artifact_store().read_bytes(run_id, f"jobs/{job_id}.json")
    except ArtifactNotFound:
        raise NotFound(f"No such job: {job_id}") from None
    return json.loads(data)


def _seconds_since(timestamp: str) -> float:
    import datetime

    then = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def _job_ended_without_result(job: ActiveJob) -> bool:
    """True if Fabric reports the notebook run instance as finished but no
    result file exists (a crash, a Monitoring-hub cancel, a capacity failure).
    Never raises: an unreachable Fabric or missing home-workspace settings just
    means this check can't run yet, so the active job is left as is."""
    if job.job_instance_id is None:
        return False
    try:
        from app.config import NEXORA_HOME_WORKSPACE_ID, NEXORA_STAGE_RUNNER_ID
        from app.connectors.fabric_api import FabricApiClient

        if not (NEXORA_HOME_WORKSPACE_ID and NEXORA_STAGE_RUNNER_ID):
            return False
        instance = FabricApiClient().get_job_instance(
            NEXORA_HOME_WORKSPACE_ID, NEXORA_STAGE_RUNNER_ID, job.job_instance_id
        )
    except Exception:
        return False
    return instance.get("status") in ("Completed", "Failed", "Cancelled", "Deduped")


def reconcile_active_job(store: RunStore, run: Run) -> Run:
    """FR-7, checked whenever a run is read. Mutates nothing when the job is
    still legitimately in flight."""
    job = run.active_job
    if job is None or job.status not in ("queued", "running"):
        return run

    if job.job_instance_id is None:
        if _seconds_since(job.started_at) > RESERVATION_TIMEOUT_SECONDS:
            return _finish(store, run, job, "cancelled")
        return run

    try:
        data = get_artifact_store().read_bytes(run.run_id, f"jobs/{job.job_id}.json")
    except ArtifactNotFound:
        if _job_ended_without_result(job):
            return _finish(
                store,
                run,
                job,
                "failed",
                error={
                    "error": {
                        "code": "JOB_FAILED",
                        "message": "The notebook job ended without writing a result",
                        "details": {"job_instance_id": job.job_instance_id},
                    }
                },
            )
        return run

    body = json.loads(data)
    status = "succeeded" if body.get("status", 500) < 400 else "failed"
    return _finish(store, run, job, status, error=body.get("body") if status == "failed" else None)


def _finish(store: RunStore, run: Run, job: ActiveJob, status: str, error: dict | None = None) -> Run:
    job.status = status
    job.finished_at = utc_now()
    if error is not None:
        job.error = error
    run.updated_at = utc_now()
    store.save(run)
    return run
