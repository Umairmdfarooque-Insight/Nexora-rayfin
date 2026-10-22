"""JSON log lines with the correlation ID, and the rule that content is never logged (T-704).

A line is `{"ts", "level", "run_id", "stage", "msg", ...}` (`run_id` and `stage` only inside a run).
Prompts, model answers, tokens and secrets are never logged in full: `redact` gives their length and a
hash, which is enough to tell two texts apart and to match a line to a saved artifact.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from app.observability.context import current_job_id, current_run_id, current_stage

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "run_id", "stage", "job_id"}


def redact(text: str | None) -> dict:
    """What may be logged about a text: its length and a short SHA-256, never the text."""
    value = text or ""
    return {"len": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]}


class CorrelationFilter(logging.Filter):
    """Adds the current run and stage to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or current_run_id()
        record.stage = getattr(record, "stage", None) or current_stage()
        record.job_id = getattr(record, "job_id", None) or current_job_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
        }
        for key in ("run_id", "stage", "job_id"):
            value = getattr(record, key, None)
            if value:
                line[key] = value
        line["msg"] = record.getMessage()
        line["logger"] = record.name
        for key, value in record.__dict__.items():  # fields passed with `extra=`
            if key not in _RESERVED and not key.startswith("_"):
                line[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            line["error"] = record.exc_info[0].__name__  # the type only: a message can carry content
        return json.dumps(line, ensure_ascii=False, default=str)


def configure_logging(logger_name: str = "app", level: int = logging.INFO) -> logging.Logger:
    """JSON lines on stderr for the app's own loggers (third-party libraries stay quiet)."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_nexora", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler()
    handler._nexora = True  # type: ignore[attr-defined]
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger
