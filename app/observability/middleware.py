"""The request middleware (T-704): binds `run_id` for every call under `/runs/{id}` and records a stage span
for each state-changing one.

The stage name comes from the path after the run id, keeping the literal lowercase segments:
`POST /runs/{id}/compile` is `stage.compile`, `POST /runs/{id}/model/validate` is `stage.model.validate`,
`POST /runs/{id}/model/bridges/{bridge}/generate` is `stage.model.bridges.generate`. A refusal (4xx) is the
outcome `rejected`, a server error (5xx or an exception) is `error` and counted. Reads bind the run for the
logs but make no span. Anything outside a run (`/health`) has no run_id and no stage span.
"""

import logging
import re
import time

from fastapi import Request

from app.observability import telemetry
from app.observability.context import bind

logger = logging.getLogger("app.request")
RUN_PATH = re.compile(r"^/runs/([0-9a-fA-F-]{36})(?:/(.*))?$")
SEGMENT = re.compile(r"^[a-z][a-z-]*$")


def stage_name(rest: str | None) -> str:
    """`model/bridges/abc/generate` -> `model.bridges.generate`; no path after the run is `run`."""
    parts = [p for p in (rest or "").split("/") if SEGMENT.match(p)]
    return ".".join(parts) or "run"


def outcome_for(status: int) -> str:
    return "error" if status >= 500 else "rejected" if status >= 400 else "success"


async def correlation(request: Request, call_next):
    match = RUN_PATH.match(request.url.path)
    if match is None:
        return await call_next(request)
    run_id, stage = match.group(1), stage_name(match.group(2))
    started = time.perf_counter()

    def log(status: int) -> None:
        logger.info(
            "request",
            extra={"method": request.method, "path": request.url.path, "status": status, "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
        )

    if request.method == "GET":
        with bind(run_id=run_id, stage=stage):
            response = await call_next(request)
            log(response.status_code)
            return response
    with telemetry.stage_span(stage, run_id=run_id) as span:
        response = await call_next(request)
        span.set_outcome(outcome_for(response.status_code))
        span.set("http.status_code", response.status_code)
        if response.status_code >= 500:
            telemetry.record_error(stage)
        log(response.status_code)
        return response
