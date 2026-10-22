"""The scope guard for targeted regeneration (T-701), in the manner of the repair scope guard (T-505).

A regeneration may only change the files of the objects it was asked to regenerate. The new folder is
compared with the version in place before it is kept; any other difference drops the attempt.
"""

from pathlib import Path


class ScopeViolation(Exception):
    """A regeneration would change files outside its impact set; it was dropped."""

    def __init__(self, what: str, files: list[str]):
        self.files = list(files)
        shown = ", ".join(self.files[:10]) + (" …" if len(self.files) > 10 else "")
        super().__init__(f"Regenerating the {what} would change files outside the stale objects, so it was dropped: {shown}")


def tree(folder: Path) -> dict[str, bytes]:
    """Every file of `folder` by relative path, without `renders/` (page images are not part of a report)."""
    folder = Path(folder)
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.relative_to(folder).parts[0] != "renders"
    }


def changed_files(previous: Path, new: Path) -> list[str]:
    """The relative paths that differ between two folders, were added or were removed."""
    a, b = tree(previous), tree(new)
    return sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))


def outside(changed: list[str], allowed: set[str] | None) -> list[str]:
    """The changed files that `allowed` doesn't cover. An entry ending in `/` covers everything under
    it; `None` means no restriction."""
    if allowed is None:
        return []
    prefixes = tuple(a for a in allowed if a.endswith("/"))
    return [f for f in changed if f not in allowed and not f.startswith(prefixes)]


def check(what: str, previous: Path | None, new: Path, allowed: set[str] | None) -> None:
    """Raise ScopeViolation if `new` differs from `previous` outside `allowed`."""
    if allowed is None or previous is None or not Path(previous).is_dir():
        return
    stray = outside(changed_files(previous, new), allowed)
    if stray:
        raise ScopeViolation(what, stray)
