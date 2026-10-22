"""The `dax_measure` stage graph (T-503): one invocation per measure.

propose_dax -> precheck_references -> (retry propose_dax with the errors, at most 2 attempts)

Routing uses only the pre-check's result and the attempt count. If both attempts are rejected the
graph ends after precheck_references with no proposal and the caller reports the reasons. The
graph never touches run state or files.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.model.dax_generator import (
    MAX_ATTEMPTS,
    SYSTEM_PROMPT,
    LlmCall,
    build_user_prompt,
    check_proposal,
    llm_schema,
)


class DaxState(TypedDict, total=False):
    kpi: Any  # Kpi
    spec: Any  # ModelSpec
    measure: str
    attempt: int
    errors: list[str]
    user: str  # the last request, so the stage can key its cache on it
    raw: str
    attempts_log: list[dict]
    proposal: Any  # DaxProposal | None


def build_dax_measure_graph(llm: LlmCall):
    """`llm(system, user, schema) -> raw JSON text` is passed in, so tests can fake it."""
    schema = llm_schema()

    def propose_dax(state: DaxState) -> dict:
        user = build_user_prompt(state["kpi"], state["spec"], state["measure"], state.get("errors"))
        raw = llm(SYSTEM_PROMPT, user, schema)
        return {"raw": raw, "user": user, "attempt": state.get("attempt", 0) + 1}

    def precheck_references(state: DaxState) -> dict:
        proposal, errors = check_proposal(state["raw"], state["kpi"], state["spec"], state["measure"])
        log = [*state.get("attempts_log", []), {"attempt": state["attempt"], "raw": state["raw"], "errors": errors}]
        return {"proposal": proposal, "errors": errors, "attempts_log": log}

    def after_precheck(state: DaxState) -> str:
        if state.get("proposal") is not None:
            return END
        if state["attempt"] < MAX_ATTEMPTS:
            return "propose_dax"
        return END

    graph = StateGraph(DaxState)
    graph.add_node("propose_dax", propose_dax)
    graph.add_node("precheck_references", precheck_references)
    graph.add_edge(START, "propose_dax")
    graph.add_edge("propose_dax", "precheck_references")
    graph.add_conditional_edges("precheck_references", after_precheck, ["propose_dax", END])
    return graph.compile()
