"""The relationship stage service (T-303): collect candidate joins, make sure the tables they
use are profiled, measure each candidate and store the evidence in the inventory.

A plain function, not a graph. The LLM only proposes candidates; deterministic code measures,
rejects and rates them. The run state never changes here (T-304's Gate G2 does that).
"""

import logging
from pathlib import Path
from typing import Any, Callable

import pyodbc

from app.connectors.fabric_api import FabricUpstreamError
from app.contracts.metadata_inventory import RelationshipEvidence
from app.discovery.data_profiler import profile_tables
from app.discovery.relationship_candidates import (
    MAX_CANDIDATES,
    Candidate,
    RelationshipLlm,
    heuristic_candidates,
    llm_candidates,
)
from app.discovery.relationship_evidence import load_thresholds, measure, resolve
from app.llm.errors import LlmInvalidOutput, LlmTimeout, LlmUpstreamError, PromptRejected
from app.store.base import RunStore
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.harvest_stage import ScopeMissing, load_inventory, save_inventory
from app.workflow.profile_stage import PROFILE_STATES
from app.workflow.run import RunScope

logger = logging.getLogger(__name__)

ANALYZE_STATES = PROFILE_STATES


def _table_key(table) -> str:
    return f"{table.schema_name}.{table.table_name}"


def analyze_run(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    sql_connector_for_scope: Callable[[RunScope], Any],
    llm: RelationshipLlm | None = None,
    extra: list[Candidate] | None = None,
) -> tuple[list[RelationshipEvidence], list[str]]:
    """Analyze the run's candidate relationships. Returns the evidence and any warnings.

    `extra` are candidates named by the user (source "user"). The previous evidence is
    replaced. If the LLM fails, the heuristic and user candidates are still analyzed and a
    warning says so.
    """
    run = store.get(run_id)
    if run.state not in ANALYZE_STATES:
        raise WrongState("Analyzing relationships", run.state, ANALYZE_STATES)
    if run.scope is None or run.scope.sql_endpoint is None:
        raise ScopeMissing(
            "The run's Lakehouse has no SQL endpoint, so relationships can't be measured yet."
        )
    inventory = load_inventory(runs_root, run_id)
    thresholds = load_thresholds()  # fails with CONFIG_ERROR before any query or LLM call

    warnings: list[str] = []
    candidates = heuristic_candidates(inventory)
    if llm is not None:
        try:
            candidates += llm_candidates(load_current_contract(runs_root, run_id), inventory, llm)
        except (LlmInvalidOutput, LlmTimeout, LlmUpstreamError, PromptRejected) as exc:
            logger.warning("Relationship suggestions skipped for run %s: %s", run_id, type(exc).__name__)
            warnings.append(
                "The model's relationship suggestions were skipped "
                f"({type(exc).__name__}); only naming heuristics and your candidates were analyzed."
            )
    candidates += extra or []
    if len(candidates) > MAX_CANDIDATES:
        warnings.append(
            f"{len(candidates)} candidates found; only the first {MAX_CANDIDATES} were analyzed."
        )
        candidates = candidates[:MAX_CANDIDATES]

    connector = sql_connector_for_scope(run.scope)
    try:
        # Profile the tables the candidates use, if they have no profile yet.
        needed: dict[str, Any] = {}
        for c in candidates:
            resolved, _ = resolve(inventory, c)
            if resolved is not None:
                for table in (resolved[0], resolved[2]):
                    if table.profile is None:
                        needed[_table_key(table)] = table
        if needed:
            profiles = profile_tables(connector, inventory, list(needed))
            for key, table in needed.items():
                table.profile = profiles[key]

        evidence = measure(connector, inventory, candidates, thresholds)
    except pyodbc.Error:
        raise FabricUpstreamError("Could not connect to the Lakehouse SQL endpoint") from None

    inventory.relationship_evidence = evidence
    save_inventory(store, runs_root, run, inventory)
    return evidence, warnings


def summarize(evidence: list[RelationshipEvidence]) -> dict[str, int]:
    def count(level: str) -> int:
        return sum(1 for e in evidence if e.status == "candidate" and e.confidence == level)

    return {
        "candidates": sum(1 for e in evidence if e.status == "candidate"),
        "high": count("high"),
        "medium": count("medium"),
        "low": count("low"),
        "rejected": sum(1 for e in evidence if e.status == "rejected"),
    }


def list_relationships(runs_root: Path, run_id: str) -> list[RelationshipEvidence]:
    return load_inventory(runs_root, run_id).relationship_evidence
