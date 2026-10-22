"""The audit trail of a run (T-703, FR-022): a tamper-evident event log and the timeline built from it.

`runs/<id>/events.jsonl` is append-only, one event per line:
`{"seq", "at", "type", "actor", "artifact", "version", "hash", "summary", "key", "prev_event_hash"}`.
Each event holds the hash of the line before it (the first holds a fixed genesis value), so editing,
removing or reordering a line breaks the chain from the next event on. `sync` derives the events from
what the run already keeps (state history, approvals, artifact registrations, gate results, validation
reports, deployments and returns) and appends those that aren't logged yet, in time order; each event's
`key` says what it came from, so a sync never logs the same thing twice. The log is a record: once an
event is in it, a later change to its source doesn't rewrite it.

**Fabric rework (T-703).** `events.jsonl` itself is read and written through the `ArtifactStore`
(`app.store.factory.get_artifact_store()`), one `append_line` call per new event, so it stays valid when a
UDF and a job both record events: `append_line` takes the run's lease for the duration of writing that one
line (T-101). Everything else `derive()` reads (gate results, `ValidationReport.json`) still goes through
the local `Path` in `runs_root` -- the same known gap as every other stage module in CLAUDE.md's "Target
hosting" section, out of this task's own scope. `append_line`'s lease covers only the write itself, not the
read-then-write of one `sync` iteration, so two writers racing between `sync`'s read and its append could
still produce a line with a `prev_event_hash` that was already stale by the time it landed; `sync` reduces
this window by re-reading and appending one event at a time rather than batching, and `verify` would still
catch the resulting broken chain (`chain_valid: false`) rather than silently losing it.

Limit: a chain can't show that lines were cut off the end, only that the lines still there are intact.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.contracts.validation_report import load_report
from app.store.artifacts import NotFound as ArtifactNotFound
from app.store.base import RunStore
from app.store.factory import get_artifact_store
from app.workflow import returns
from app.workflow.approval import list_approvals
from app.workflow.run import Run

logger = logging.getLogger(__name__)

EVENTS_FILE = "events.jsonl"
GENESIS = "sha256:" + "0" * 64
FIELDS = ("seq", "at", "type", "actor", "artifact", "version", "hash", "summary", "key", "prev_event_hash")


def event_hash(event: dict[str, Any]) -> str:
    text = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_lines(runs_root: Path, run_id: str) -> list[str]:
    """`events.jsonl` through the artifact store (T-703's Fabric rework); `runs_root` is accepted only for
    the signature's sake (every other `derive()` read still uses it, see the module docstring)."""
    try:
        data = get_artifact_store().read_bytes(run_id, EVENTS_FILE)
    except ArtifactNotFound:
        return []
    return [line for line in data.decode("utf-8").splitlines() if line.strip()]


def verify(lines: list[str]) -> tuple[list[dict | None], int | None]:
    """The parsed events (None for a line that isn't an event) and the first `seq` where the chain breaks
    (a line that can't be read, a wrong `seq`, or a `prev_event_hash` that isn't the hash of the line
    before), or None if the chain is intact."""
    events: list[dict | None] = []
    broken: int | None = None
    previous = GENESIS
    for index, line in enumerate(lines):
        expected = index + 1
        try:
            event = json.loads(line)
            ok = isinstance(event, dict) and all(f in event for f in FIELDS)
        except ValueError:
            event, ok = None, False
        events.append(event if ok else None)
        if broken is None and (not ok or event["seq"] != expected or event["prev_event_hash"] != previous):
            broken = expected
        if ok:
            previous = event_hash(event)
        else:
            previous = "unreadable"
    return events, broken


# --- deriving the events ------------------------------------------------------------------------------------


def _candidate(at: str, type_: str, actor: str, key: str, summary: str, artifact=None, version=None, hash_=None) -> dict:
    return {"at": at, "type": type_, "actor": actor or "system", "artifact": artifact, "version": version, "hash": hash_, "summary": summary, "key": key}


def derive(store: RunStore, runs_root: Path, run: Run) -> list[dict]:
    """Everything the run knows about, as events without `seq` and `prev_event_hash`, oldest first."""
    run_dir = Path(runs_root) / run.run_id
    out: list[dict] = []
    for i, h in enumerate(run.history):
        text = f"{h.from_.value if h.from_ else 'start'} → {h.to.value}: {h.reason}"
        out.append(_candidate(h.at, "state", h.actor, f"history:{i}", text))
    for r in list_approvals(store, runs_root, run.run_id):
        who = f" as {r.role}" if r.role else ""
        note = f": {r.comment[:200]}" if r.comment else ""
        target = f" ({json.dumps(r.target, sort_keys=True)})" if r.target else ""
        out.append(_candidate(r.at, "approval", r.user.get("upn", ""), f"approval:{r.approval_id}",
                              f"{r.action.value} {r.artifact}{target}{who}{note}", r.artifact, r.artifact_version, r.artifact_hash))
    for name, ref in run.artifacts.items():
        out.append(_candidate(ref.updated_at, "artifact", "system", f"artifact:{name}:{ref.version}:{ref.hash}:{ref.updated_at}",
                              f"{name} v{ref.version} is {ref.status}", name, ref.version, ref.hash))
    for path in sorted((run_dir / "gates").glob("*.json")):
        try:
            g = json.loads(path.read_text(encoding="utf-8"))
            failed = [c["id"] for c in g.get("checks", []) if c.get("status") == "fail"]
            out.append(_candidate(g["checked_at"], "gate", "system", f"gate:{path.name}",
                                  f"Gate {g['gate']} {'passed' if g['passed'] else 'failed' + (': ' + ', '.join(failed) if failed else '')}",
                                  g.get("artifact"), None, g.get("artifact_hash")))
        except (OSError, ValueError, KeyError):
            continue
    for v in load_report(run_dir / "ValidationReport.json", run.run_id).runs:
        failed = sum(1 for c in v.checks if c.status == "fail")
        out.append(_candidate(v.started_at, "validation", "system", f"validation:{v.artifact}:{v.started_at}:{v.artifact_hash}:{v.attempt}",
                              f"{v.artifact} validation {'passed' if v.passed else f'failed ({failed} check(s))'}", v.artifact, v.artifact_version, v.artifact_hash))
    d = run.deployment
    if d and d.get("deployment_id"):
        out.append(_candidate(d.get("started_at") or run.updated_at, "deployment", "system", f"deployment:{d['deployment_id']}:started",
                              f"Deployment to workspace {d.get('target_workspace_id')} started"))
        if d.get("outcome"):
            hashes = ", ".join(f"{i['type']} {i['hash'][:19]}…" for i in d.get("items", []))
            out.append(_candidate(d.get("finished_at") or run.updated_at, "deployment", "system", f"deployment:{d['deployment_id']}:{d['outcome']}",
                                  f"Deployment {d['outcome']}" + (f" ({hashes})" if hashes else "") + (f": {d['error']}" if d.get("error") else "")))
    for r in returns.list_records(runs_root, run.run_id):
        who = (r.user or {}).get("upn", "")
        text = f"Returned to {r.to}: {r.reason}" if r.event == "returned" else f"Return resolved: {r.reason}"
        out.append(_candidate(r.at, "return", who, f"return:{r.return_id}:{r.event}", text))
    return sorted(out, key=lambda e: e["at"])  # a stable sort: ties keep the order above


def sync(store: RunStore, runs_root: Path, run_id: str) -> int:
    """Append the events that aren't in the log yet, one `ArtifactStore.append_line` call at a time (see
    the module docstring). Returns how many were appended."""
    run = store.get(run_id)
    artifact_store = get_artifact_store()
    appended = 0
    while True:
        lines = read_lines(runs_root, run_id)
        events, _ = verify(lines)
        logged = {e["key"] for e in events if e}
        new = [c for c in derive(store, runs_root, run) if c["key"] not in logged]
        if not new:
            return appended
        candidate = new[0]
        last = next((e for e in reversed(events) if e), None)
        previous = event_hash(last) if last and events[-1] is not None else (GENESIS if not lines else "unreadable")
        event = {"seq": len(lines) + 1, **candidate, "prev_event_hash": previous}
        artifact_store.append_line(run_id, EVENTS_FILE, json.dumps({f: event[f] for f in FIELDS}, ensure_ascii=False))
        appended += 1


def record(store: RunStore, runs_root: Path, run_id: str) -> None:
    """`sync` that never raises: an action must not fail because its audit line couldn't be written."""
    try:
        sync(store, runs_root, run_id)
    except Exception:
        logger.exception("Couldn't update the audit log of run %s", run_id)


def audit(store: RunStore, runs_root: Path, run_id: str, offset: int = 0, limit: int = 200) -> dict:
    """`GET /runs/{id}/audit`: a page of events, the total and whether the chain is intact. If it isn't,
    `first_broken_seq` is where it breaks."""
    sync(store, runs_root, run_id)
    lines = read_lines(runs_root, run_id)
    events, broken = verify(lines)
    page = events[offset : offset + limit]
    shown = [e if e is not None else {"seq": offset + i + 1, "summary": "(unreadable line)", "unreadable": True} for i, e in enumerate(page)]
    return {"events": shown, "total": len(lines), "chain_valid": broken is None, "first_broken_seq": broken}
