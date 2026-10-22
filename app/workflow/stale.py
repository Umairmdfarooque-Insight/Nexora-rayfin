"""Stale artifacts (T-701, business rule 6): an artifact built from something that has since changed.

This module has no imports from the rest of the workflow, so run.py and every stage can use it.
`impact.py` decides which artifacts are stale and marks them (`ArtifactRef.status == "stale"`); the
stages only refuse to build on them, with `assert_fresh`.
"""

STALE = "stale"


class StaleArtifacts(Exception):
    """An action needs artifacts that are stale. Nothing was changed."""

    def __init__(self, action: str, names: list[str]):
        self.action = action
        self.names = list(names)
        super().__init__(
            f"{action} is blocked because these artifacts are stale: {', '.join(self.names)}. "
            "Regenerate them first (POST /runs/{id}/regenerate-stale)"
        )


def stale_names(artifacts: dict) -> list[str]:
    """The names of the artifacts whose status is `stale`, sorted."""
    return sorted(name for name, ref in artifacts.items() if ref.status == STALE)


def assert_fresh(artifacts: dict, names: tuple[str, ...], action: str) -> None:
    """Raise StaleArtifacts if any of `names` is stale. Artifacts a run doesn't have are ignored."""
    found = [n for n in names if n in artifacts and artifacts[n].status == STALE]
    if found:
        raise StaleArtifacts(action, found)
