"""Harvest the tables and columns of a run's scope (T-301, FR-005).

Read-only and deterministic: no LLM. The connector is the only source of names, so no
schema or table name is hardcoded here.
"""

import logging
from typing import Any

import requests

from app.workflow.scope import SchemaNotFound
from app.connectors.fabric_api import (
    FabricError,
    FabricForbidden,
    FabricNotFound,
    FabricRateLimited,
    FabricTimeout,
    FabricUpstreamError,
)

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 50
NO_TABLES_WARNING = "No tables were found in the scoped schemas. Gate G2 will fail until tables exist."


def classify(exc: Exception) -> tuple[str, str]:
    """An error code and a message that is safe to store, for a failed OneLake call."""
    if isinstance(exc, requests.Timeout):
        return "UPSTREAM_TIMEOUT", "The Lakehouse did not respond in time"
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            return "FORBIDDEN", f"Access was denied (HTTP {status})"
        if status == 404:
            return "NOT_FOUND", "The table was not found (HTTP 404)"
        if status == 429:
            return "RATE_LIMITED", "The Lakehouse is throttling requests (HTTP 429)"
        return "UPSTREAM_ERROR", f"The Lakehouse returned HTTP {status}"
    return "UPSTREAM_ERROR", f"The Lakehouse could not be read ({type(exc).__name__})"


def to_fabric_error(exc: Exception) -> FabricError:
    """The typed error for a failed call that aborts the whole harvest."""
    code, message = classify(exc)
    kind = {
        "FORBIDDEN": FabricForbidden,
        "NOT_FOUND": FabricNotFound,
        "RATE_LIMITED": FabricRateLimited,
        "UPSTREAM_TIMEOUT": FabricTimeout,
    }.get(code, FabricUpstreamError)
    if kind is FabricRateLimited:
        retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
        return FabricRateLimited(message, retry_after=int(retry_after) if str(retry_after).isdigit() else 5)
    return kind(message)


def resolve_schemas(connector: Any, requested: list[str]) -> list[str]:
    """The Lakehouse's spelling of each requested schema, or of all schemas if none were
    requested. Raises SchemaNotFound (app.workflow.scope) for an unknown schema."""
    try:
        available = {s["name"].lower(): s["name"] for s in connector.list_schemas()}
    except requests.RequestException as exc:
        raise to_fabric_error(exc) from None
    if not requested:
        return sorted(available.values(), key=str.casefold)
    resolved = []
    for name in dict.fromkeys(requested):
        if name.lower() not in available:
            raise SchemaNotFound(name)
        resolved.append(available[name.lower()])
    return resolved


def _columns(metadata: dict) -> list[dict]:
    columns = []
    for column in metadata.get("columns") or []:
        columns.append(
            {
                "name": column.get("name"),
                "data_type": column.get("type_name"),
                "nullable": column.get("nullable"),
                "description": column.get("comment") or None,
            }
        )
    return columns


def harvest_tables(connector: Any, schemas: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (tables, errors, warnings). A table that fails is recorded in `errors` and the
    others are still harvested. Listing a schema that fails aborts with a typed error."""
    tables: list[dict] = []
    errors: list[dict] = []
    warnings: list[str] = []

    for schema in schemas:
        try:
            listed = connector.list_tables(schema)
        except requests.RequestException as exc:
            raise to_fabric_error(exc) from None

        for index, entry in enumerate(listed, start=1):
            name = entry["name"]
            try:
                metadata = connector.get_table_metadata(schema, name)
            except requests.RequestException as exc:
                code, message = classify(exc)
                errors.append({"table": f"{schema}.{name}", "code": code, "message": message})
                continue

            columns = _columns(metadata)
            table_errors: list[dict] = []
            if not columns:
                table_errors.append(
                    {"code": "NO_COLUMNS", "message": "The table returned no column metadata"}
                )
                errors.append(
                    {"table": f"{schema}.{name}", "code": "NO_COLUMNS", "message": table_errors[0]["message"]}
                )
            tables.append(
                {
                    "schema": schema,
                    "table_name": name,
                    "description": metadata.get("comment") or None,
                    "columns": columns,
                    "profile": None,
                    "errors": table_errors,
                }
            )
            if index % PROGRESS_EVERY == 0:
                logger.info("Harvested %d of %d tables in schema %s", index, len(listed), schema)

    tables.sort(key=lambda t: (t["schema"].casefold(), t["table_name"].casefold()))
    if not tables:
        warnings.append(NO_TABLES_WARNING)
    return tables, errors, warnings
