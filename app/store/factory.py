"""Pick the RunStore (RUN_STORE) and ArtifactStore (ARTIFACT_STORE)."""

import contextvars

from app.config import (
    get_optional_setting,
    get_required_setting,
)
from app.store.artifacts import ArtifactStore
from app.store.base import RunStore

_artifact_store_override: contextvars.ContextVar["ArtifactStore | None"] = contextvars.ContextVar(
    "artifact_store_override", default=None
)


def set_artifact_store_override(store: "ArtifactStore | None") -> None:
    """`nexora_api`'s `function_app.py` uses this: a UDF can't read `nexora_settings`
    at all (no `notebookutils`, confirmed against a live tenant, 2026-11-02), so
    `ARTIFACT_STORE`/`NEXORA_HOME_WORKSPACE_ID`/`NEXORA_CONTROL_LAKEHOUSE_ID` always
    read as unset inside it and `get_artifact_store()` below would always fall back
    to `local` -- a store nothing else can see. Instead the UDF builds a
    `UdfLakehouseArtifactStore` from its own Manage Connections-injected
    `fn.FabricLakehouseClient` (the same pattern `app.fabric.deps.set_store_override`
    already uses for the SQL-backed `RunStore`) and sets it here for the duration of
    one invocation. A context variable (not a plain global) keeps concurrent
    invocations in the same worker process isolated."""
    _artifact_store_override.set(store)


def get_run_store() -> RunStore:
    kind = (get_optional_setting("RUN_STORE", "file") or "file").lower()

    if kind == "file":
        from app.store.file_store import FileRunStore

        return FileRunStore()

    if kind == "fabric_sql":
        from app.store.sql_store import SqlRunStore

        return SqlRunStore(
            get_required_setting("FABRIC_CONTROL_DB_SERVER"),
            get_required_setting("FABRIC_CONTROL_DB_NAME"),
        )

    raise ValueError(
        f"Invalid RUN_STORE: {kind!r}. Valid values: file, fabric_sql"
    )


def get_artifact_store() -> ArtifactStore:
    override = _artifact_store_override.get()
    if override is not None:
        return override

    kind = (get_optional_setting("ARTIFACT_STORE", "local") or "local").lower()

    if kind == "local":
        from app.store.artifacts import LocalArtifactStore

        return LocalArtifactStore()

    if kind == "onelake":
        from app.store.artifacts import OneLakeArtifactStore

        return OneLakeArtifactStore(
            get_required_setting("NEXORA_HOME_WORKSPACE_ID"),
            get_required_setting("NEXORA_CONTROL_LAKEHOUSE_ID"),
        )

    raise ValueError(
        f"Invalid ARTIFACT_STORE: {kind!r}. Valid values: local, onelake"
    )
