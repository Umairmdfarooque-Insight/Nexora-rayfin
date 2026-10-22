"""The `compile` stage graph (T-202).

llm_compile -> validate_schema -> (retry llm_compile with the errors, at most 2 LLM attempts)
            -> apply_critical_terms -> apply_locked_facts

Routing uses only validator output and the attempt count. If both attempts are invalid the
graph ends after validate_schema with no contract and the caller reports LLM_INVALID_OUTPUT.
Every successful path ends in apply_critical_terms then apply_locked_facts.
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.compiler.critical_terms import CriticalTerms, apply_critical_terms, apply_locked_facts
from app.compiler.requirement_compiler import (
    SYSTEM_PROMPT,
    build_user_prompt,
    llm_schema,
    parse_draft,
)

MAX_ATTEMPTS = 2


class CompileState(TypedDict, total=False):
    run: Any  # Run
    previous: Any  # BusinessContract | None
    attempt: int
    errors: list[str]
    raw: str
    attempts_log: list[dict]
    draft: Any  # BusinessContract | None
    contract: Any  # BusinessContract | None
    warnings: list[str]
    guidance: str | None


def build_compile_graph(llm: Callable[[str, str, dict], str], config: CriticalTerms):
    """`llm(system, user, schema) -> raw JSON text` is passed in, so tests can fake it."""
    schema = llm_schema()

    def llm_compile(state: CompileState) -> dict:
        user = build_user_prompt(
            state["run"], state.get("previous"), state.get("errors", []), state.get("guidance")
        )
        raw = llm(SYSTEM_PROMPT, user, schema)
        return {"raw": raw, "attempt": state.get("attempt", 0) + 1}

    def validate_schema(state: CompileState) -> dict:
        draft, errors = parse_draft(state["raw"], state["run"], state.get("previous"))
        log = [
            *state.get("attempts_log", []),
            {"attempt": state["attempt"], "raw": state["raw"], "errors": errors},
        ]
        if draft is None:
            return {"draft": None, "errors": errors, "attempts_log": log}
        return {"draft": draft, "errors": [], "attempts_log": log}

    def after_validate(state: CompileState) -> str:
        if state.get("draft") is not None:
            return "apply_critical_terms"
        if state["attempt"] < MAX_ATTEMPTS:
            return "llm_compile"
        return END

    def critical_terms(state: CompileState) -> dict:
        run = state["run"]
        text = "\n".join([run.prompt, *run.constraints])
        contract = apply_critical_terms(state["draft"], text, state.get("previous"), config)
        return {"contract": contract}

    def locked_facts(state: CompileState) -> dict:
        warnings: list[str] = []
        contract = apply_locked_facts(
            state["contract"], state.get("previous"), config, warnings
        )
        return {"contract": contract, "warnings": [*state.get("warnings", []), *warnings]}

    graph = StateGraph(CompileState)
    graph.add_node("llm_compile", llm_compile)
    graph.add_node("validate_schema", validate_schema)
    graph.add_node("apply_critical_terms", critical_terms)
    graph.add_node("apply_locked_facts", locked_facts)
    graph.add_edge(START, "llm_compile")
    graph.add_edge("llm_compile", "validate_schema")
    graph.add_conditional_edges(
        "validate_schema",
        after_validate,
        ["llm_compile", "apply_critical_terms", END],
    )
    graph.add_edge("apply_critical_terms", "apply_locked_facts")
    graph.add_edge("apply_locked_facts", END)
    return graph.compile()
