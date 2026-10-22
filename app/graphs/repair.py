"""The `repair` stage graph (T-505, reused by T-603 for reports).

classify -> deterministic_fix -> llm_fix -> scope_guard -> validate -> check_progress
              ^                                                            |
              +------------------------- (loop) --------------------------+

The graph is generic: everything artifact-specific (what a check means, how an object is fixed,
which files a fix may touch, how a version is written and validated) is an adapter in the state,
so a report adapter can reuse it. Routing uses only policy classes, fingerprints and the attempt
counter against `max_attempts`, never free model text. The graph never changes run state or the
manifest: it writes new version files and appends to ValidationReport.json (through the adapter's
`validate`), and the repair service registers artifacts and makes the transition afterwards.

Outcomes: passed | limit_reached | no_progress | user_input_only.
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

NODES_PER_ATTEMPT = 6


class RepairState(TypedDict, total=False):
    run_id: str
    artifact: str
    adapter: Any  # the artifact adapter (model: app/repair/model_adapter.py)
    policy: Any  # RepairPolicy
    version: int
    errors: list  # the failed checks of the latest validation
    classified: list  # ClassifiedError of `errors`
    attempt: int
    attempts: list[dict]
    seen_fingerprints: list[str]
    discarded: bool  # this attempt changed nothing or was dropped by the scope guard
    note: str | None
    outcome: str | None


def fingerprints(items) -> set[str]:
    return {i.fingerprint for i in items}


def build_repair_graph(llm: Callable[[str, str, dict], str] | None = None):
    """`llm(system, user, schema) -> raw JSON text` is passed in, so tests can fake it."""

    def classify(state: RepairState) -> dict:
        classified = state["adapter"].classify(state["errors"], state["policy"])
        seen = list(dict.fromkeys([*state.get("seen_fingerprints", []), *(c.fingerprint for c in classified)]))
        update: dict = {"classified": classified, "seen_fingerprints": seen, "discarded": False, "note": None}
        if classified and all(c.classification == "user_input" for c in classified):
            update["outcome"] = "user_input_only"
        return update

    def after_classify(state: RepairState) -> str:
        return END if state.get("outcome") else "deterministic_fix"

    def deterministic_fix(state: RepairState) -> dict:
        state["adapter"].begin_attempt()
        state["adapter"].deterministic_fix([c for c in state["classified"] if c.classification == "deterministic"])
        return {"attempt": state.get("attempt", 0) + 1}

    def llm_fix(state: RepairState) -> dict:
        state["adapter"].llm_fix(llm, [c for c in state["classified"] if c.classification == "llm"])
        return {}

    def scope_guard(state: RepairState) -> dict:
        adapter = state["adapter"]
        if not adapter.has_changes():
            adapter.discard()
            return {"discarded": True, "note": "; ".join(adapter.take_notes()) or "no fix could be produced"}
        violations = adapter.scope_guard(state["classified"])
        if violations:
            adapter.discard()
            notes = adapter.take_notes()
            return {"discarded": True, "note": "; ".join([f"the attempt changed files outside the failing objects: {', '.join(violations)}", *notes])}
        return {"note": "; ".join(adapter.take_notes()) or None}

    def after_guard(state: RepairState) -> str:
        return "check_progress" if state.get("discarded") else "validate"

    def validate(state: RepairState) -> dict:
        entry = state["adapter"].validate(state["attempt"])
        return {"errors": [c for c in entry.checks if c.status == "fail"], "version": state["adapter"].version}

    def check_progress(state: RepairState) -> dict:
        adapter, policy = state["adapter"], state["policy"]
        before = fingerprints(state["classified"])
        errors = state["errors"]
        after_classified = adapter.classify(errors, policy)
        after = fingerprints(after_classified)
        seen = set(state.get("seen_fingerprints", []))
        attempts = [
            *state.get("attempts", []),
            {
                "attempt": state["attempt"],
                "fixed": sorted(before - after),
                "remaining": sorted(after),
                "classification": {c.fingerprint: c.classification for c in after_classified},
                "version": state.get("version", adapter.version),
                "discarded": bool(state.get("discarded")),
                "note": state.get("note"),
            },
        ]
        update: dict = {"attempts": attempts, "seen_fingerprints": sorted(seen | after)}
        if not after:
            update["outcome"] = "passed"
        elif all(c.classification == "user_input" for c in after_classified):
            update["outcome"] = "user_input_only"
        elif not (after - seen) and len(after) >= len(before):
            update["outcome"] = "no_progress"
        elif state["attempt"] >= state["policy"].max_attempts:
            update["outcome"] = "limit_reached"
        return update

    def after_progress(state: RepairState) -> str:
        return END if state.get("outcome") else "classify"

    graph = StateGraph(RepairState)
    for name, fn in (
        ("classify", classify),
        ("deterministic_fix", deterministic_fix),
        ("llm_fix", llm_fix),
        ("scope_guard", scope_guard),
        ("validate", validate),
        ("check_progress", check_progress),
    ):
        graph.add_node(name, fn)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", after_classify, ["deterministic_fix", END])
    graph.add_edge("deterministic_fix", "llm_fix")
    graph.add_edge("llm_fix", "scope_guard")
    graph.add_conditional_edges("scope_guard", after_guard, ["validate", "check_progress"])
    graph.add_edge("validate", "check_progress")
    graph.add_conditional_edges("check_progress", after_progress, ["classify", END])
    return graph.compile()
