"""Bind a run to a Fabric workspace and Lakehouse (T-103, FR-004).

Deterministic checks only: the identity must be able to see the workspace and
Lakehouse, and any requested schemas must exist in that Lakehouse.
"""

from typing import Any, Callable

import requests

from app.connectors.fabric_api import (
    FabricApiClient,
    FabricError,
    FabricForbidden,
    FabricNotFound,
    FabricTimeout,
    FabricUpstreamError,
)
from app.store.base import RunStore
from app.workflow.run import Run, RunScope, SqlEndpoint, utc_now
from app.workflow.states import RunState

NO_SQL_ENDPOINT_WARNING = (
    "The Lakehouse has no SQL endpoint yet. Data profiling and relationship "
    "checks need it, so wait until it is provisioned."
)


class ScopeLocked(Exception):
    def __init__(self, state: RunState):
        self.state = state
        super().__init__(
            f"The scope can't be changed in state {state.value}; "
            "metadata may already be harvested"
        )


class SchemaNotFound(Exception):
    def __init__(self, schema: str):
        self.schema = schema
        super().__init__(f"Schema not found in the Lakehouse: {schema}")


def scope_is_locked(run: Run) -> bool:
    if run.state in (RunState.CREATED, RunState.NEEDS_CLARIFICATION):
        return False
    if run.state is RunState.REQUIREMENT_APPROVED:
        return "MetadataInventory" in run.artifacts
    return True


def _schema_names(connector: Any) -> dict[str, str]:
    """Lowercased name -> name as the Lakehouse spells it."""
    try:
        schemas = connector.list_schemas()
    except requests.Timeout:
        raise FabricTimeout("The Lakehouse did not respond in time") from None
    except requests.HTTPError as error:
        status = getattr(error.response, "status_code", None)
        if status in (401, 403):
            raise FabricForbidden("Access to the Lakehouse was denied") from None
        raise FabricUpstreamError(f"The Lakehouse returned HTTP {status}") from None
    except requests.RequestException as error:
        raise FabricUpstreamError(
            f"The Lakehouse could not be reached ({type(error).__name__})"
        ) from None
    return {s["name"].lower(): s["name"] for s in schemas}


def bind_scope(
    store: RunStore,
    fabric: FabricApiClient,
    connector_for_scope: Callable[[RunScope], Any],
    run_id: str,
    workspace_id: str,
    lakehouse_id: str,
    schemas: list[str] | None = None,
) -> tuple[Run, list[str]]:
    """Verify and save a run's scope. Returns the run and any warnings.

    Raises RunNotFound, ScopeLocked, FabricForbidden, SchemaNotFound or a
    FabricError. Nothing is saved unless every check passes.
    """
    run = store.get(run_id)
    if scope_is_locked(run):
        raise ScopeLocked(run.state)

    # One call proves the identity can see both the workspace and the Lakehouse.
    # An unknown workspace is reported as forbidden, so ids can't be probed.
    try:
        lakehouses = fabric.list_lakehouses(workspace_id)
    except FabricNotFound:
        raise FabricForbidden("The workspace is not available to your identity") from None
    lakehouse = next(
        (lh for lh in lakehouses if lh["id"].lower() == lakehouse_id.lower()), None
    )
    if lakehouse is None:
        raise FabricForbidden("The Lakehouse is not available to your identity")

    endpoint = lakehouse["sql_endpoint"]
    scope = RunScope(
        workspace_id=workspace_id,
        workspace_name=_workspace_name(fabric, workspace_id),
        lakehouse_id=lakehouse["id"],
        lakehouse_name=lakehouse["display_name"],
        schemas=[],
        sql_endpoint=SqlEndpoint(**endpoint) if endpoint else None,
    )

    requested = list(dict.fromkeys(schemas or []))
    if requested:
        available = _schema_names(connector_for_scope(scope))
        resolved = []
        for name in requested:
            if name.lower() not in available:
                raise SchemaNotFound(name)
            resolved.append(available[name.lower()])
        scope.schemas = resolved

    run.scope = scope
    run.updated_at = utc_now()
    store.save(run)

    warnings = [] if scope.sql_endpoint else [NO_SQL_ENDPOINT_WARNING]
    return run, warnings


def _workspace_name(fabric: FabricApiClient, workspace_id: str) -> str | None:
    """Best effort: the name is only for display, so a failure isn't an error."""
    try:
        for workspace in fabric.list_workspaces():
            if workspace["id"].lower() == workspace_id.lower():
                return workspace["display_name"]
    except FabricError:
        pass
    return None
