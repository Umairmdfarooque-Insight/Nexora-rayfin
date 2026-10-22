"""Keeps a model's accepted answer per request, so the same inputs give the same stage output.

The key is a hash of the system prompt and the exact user message. The answer is saved only
after the stage has accepted it, so unusable output is never reused.
"""

import hashlib
import json
from pathlib import Path

from app.workflow.harvest_stage import write_json


def request_key(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n{user}".encode("utf-8")).hexdigest()


def saved_answer(run_dir: Path, name: str, key: str) -> str | None:
    try:
        saved = json.loads((Path(run_dir) / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(saved, dict) and saved.get("key") == key and isinstance(saved.get("response"), str):
        return saved["response"]
    return None


def save_answer(run_dir: Path, name: str, key: str, response: str) -> None:
    write_json(Path(run_dir) / name, {"key": key, "response": response})


def read_answers(run_dir: Path, name: str) -> dict[str, str]:
    """Every saved answer of a stage that keeps several (one per request), keyed by request."""
    try:
        data = json.loads((Path(run_dir) / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    answers = data.get("answers") if isinstance(data, dict) else None
    return {k: v for k, v in answers.items() if isinstance(v, str)} if isinstance(answers, dict) else {}


def write_answers(run_dir: Path, name: str, answers: dict[str, str]) -> None:
    write_json(Path(run_dir) / name, {"answers": answers})


class CachedLlm:
    """Replays a saved accepted answer for an identical request and remembers the last answer."""

    def __init__(self, llm, cache: dict[str, str]):
        self.llm, self.cache = llm, cache
        self.last_key: str | None = None
        self.last_raw: str | None = None

    def __call__(self, system: str, user: str, schema: dict) -> str:
        key = request_key(system, user)
        raw = self.cache.get(key)
        if raw is None:
            raw = self.llm(system, user, schema)
        self.last_key, self.last_raw = key, raw
        return raw
