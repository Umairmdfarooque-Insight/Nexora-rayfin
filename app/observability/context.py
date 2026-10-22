"""The correlation context of the current work: which run, which stage, which job (T-704, NFR-007).

`run_id` is the correlation ID. It is bound for the duration of a UDF call or a job and read by the log
formatter, the spans, the metrics and the outbound calls. `job_id` is bound in addition while a
`nexora_stage_runner` job runs (so a UDF call and the job stage it's part of both carry it). Work outside
a run (`/health`) has none, and every reader copes with that.
"""

import contextvars
from contextlib import contextmanager

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("nexora_run_id", default=None)
_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar("nexora_stage", default=None)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("nexora_job_id", default=None)


def current_run_id() -> str | None:
    return _run_id.get()


def current_stage() -> str | None:
    return _stage.get()


def current_job_id() -> str | None:
    return _job_id.get()


@contextmanager
def bind(run_id: str | None = None, stage: str | None = None, job_id: str | None = None):
    """Bind the run, the stage and/or the job for the block; what isn't given keeps its current value."""
    tokens = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    if job_id is not None:
        tokens.append((_job_id, _job_id.set(job_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
