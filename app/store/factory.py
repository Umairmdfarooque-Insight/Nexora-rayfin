"""Pick the RunStore (RUN_STORE) and ArtifactStore (ARTIFACT_STORE)."""

from app.config import (
    get_optional_setting,
    get_required_setting,
)
from app.store.artifacts import ArtifactStore
from app.store.base import RunStore


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
