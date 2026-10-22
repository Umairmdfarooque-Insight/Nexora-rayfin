"""The return log of a run (T-701): `runs/<id>/returns.jsonl`, append-only.

A *return* sends an approved run back to an earlier stage with a reason. Each return stores a
baseline: the version and hash of every artifact at that moment. Impact analysis compares the
baseline with what the artifacts are now, at object level. A return stays *open* until the run is
approved again as far as it was and nothing is stale; then a `resolved` event closes it. Earlier
versions of every artifact stay on disk, so the baseline can always be read back.

This module has no dependencies on the stage services.
"""

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RETURNS_FILE = "returns.jsonl"
TARGETS = ("REQUIREMENT_ANALYSIS", "PLANNING", "MODEL")


class ReturnRecord(BaseModel):
    event: Literal["returned", "resolved"] = "returned"
    return_id: str
    run_id: str
    at: str
    user: dict[str, str] | None = None
    to: str | None = None  # REQUIREMENT_ANALYSIS | PLANNING | MODEL
    from_state: str | None = None  # the state the run was in before the (first) open return
    new_state: str | None = None
    reason: str
    baseline: dict[str, dict[str, Any]] = Field(default_factory=dict)  # artifact -> {version, hash, path, status}
    unlocked: list[str] = Field(default_factory=list)
    approvals_before: int = 0  # how many records approvals.jsonl held: only later ones count (an order, not a clock)


def new_return_id() -> str:
    return str(uuid.uuid4())


def _path(runs_root: Path, run_id: str) -> Path:
    return Path(runs_root) / run_id / RETURNS_FILE


def append(runs_root: Path, record: ReturnRecord) -> None:
    path = _path(runs_root, record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
    with open(path, "ab") as handle:  # append only; bytes, so Windows keeps LF
        handle.write(line.encode("utf-8"))


def list_records(runs_root: Path, run_id: str) -> list[ReturnRecord]:
    path = _path(runs_root, run_id)
    if not path.is_file():
        return []
    return [
        ReturnRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def open_return(runs_root: Path, run_id: str) -> ReturnRecord | None:
    """The latest `returned` event that no `resolved` event has closed, or None."""
    current: ReturnRecord | None = None
    for record in list_records(runs_root, run_id):
        if record.event == "returned":
            current = record
        elif current is not None and record.return_id == current.return_id:
            current = None
    return current


def count_approvals(runs_root: Path, run_id: str) -> int:
    path = Path(runs_root) / run_id / "approvals.jsonl"
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if path.is_file() else 0


def reopened_after(runs_root: Path, run_id: str) -> int | None:
    """The number of approval records the run had when it was last returned (any target). Approvals of
    the model and the report before that point no longer count: a return reopens them even if the files
    come out identical. None if the run was never returned."""
    found = [r.approvals_before for r in list_records(runs_root, run_id) if r.event == "returned"]
    return found[-1] if found else None
