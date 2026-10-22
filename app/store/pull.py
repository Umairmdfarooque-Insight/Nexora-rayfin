"""Copy a run's files from the artifact store to a local folder (T-101 FR-7),
for `python -m app.cli runs pull` and for the CLI commands that need a real
folder when `ARTIFACT_STORE=onelake`. Read-only towards Fabric.
"""

from pathlib import Path

from app.store.artifacts import ArtifactStore


def pull_run(artifact_store: ArtifactStore, run_id: str, to: Path) -> int:
    """Download every file under the run's folder into `to`. Returns how many
    files were copied."""
    to = Path(to)
    to.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel in artifact_store.list(run_id, ""):
        data = artifact_store.read_bytes(run_id, rel)
        target = to / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    return count
