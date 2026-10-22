"""Reads what a PBIR folder binds (T-603): the pages, visuals, fields and filters.

The checks of Gate G5 look at the files as they are on disk, not at the plan, so a hand edit or a
bad repair shows up. The reader understands the structure `pbir_generator.py` writes (and Power BI
Desktop's equivalent): `query.queryState.<role>.projections[].field` with a Measure, Column or
HierarchyLevel expression, and `filterConfig.filters[]` with a categorical `In` condition. Anything
else it meets is kept as an `unsupported` field, which the binding check reports. Unreadable files
are skipped here: the schema check reports them.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BoundField:
    kind: str  # measure | column | hierarchy | unsupported
    table: str | None = None
    name: str | None = None  # the measure, column or hierarchy name
    level: str | None = None  # hierarchy level
    active: bool = True  # a hierarchy level that is shown (the others are reached by drilling)
    role: str = ""
    path: str = ""  # where it was found, for messages: "queryState.Category.projections[0]"

    def label(self) -> str:
        if self.kind == "measure":
            return f"measure '{self.name}'"
        if self.kind == "column":
            return f"column '{self.table}'[{self.name}]"
        if self.kind == "hierarchy":
            return f"hierarchy '{self.table}'.{self.name} level '{self.level}'"
        return f"an unsupported field expression at {self.path}"


@dataclass
class FilterRef:
    table: str | None
    column: str | None
    values: list[Any] = field(default_factory=list)
    path: str = ""


@dataclass
class VisualRead:
    id: str
    page: str
    visual_type: str | None
    fields: list[BoundField] = field(default_factory=list)
    filters: list[FilterRef] = field(default_factory=list)
    file: str = ""  # relative to the report folder


@dataclass
class PageRead:
    name: str
    display_name: str | None
    filters: list[FilterRef] = field(default_factory=list)
    visuals: list[VisualRead] = field(default_factory=list)
    file: str = ""


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entity(expression: Any) -> str | None:
    if isinstance(expression, dict):
        return ((expression.get("SourceRef") or {}).get("Entity")) if isinstance(expression.get("SourceRef"), dict) else None
    return None


def parse_field(node: Any, role: str, path: str, active: bool = True) -> BoundField:
    """One `field` expression of a projection or filter."""
    base = dict(role=role, path=path, active=active)
    if isinstance(node, dict):
        if isinstance(node.get("Measure"), dict):
            m = node["Measure"]
            return BoundField("measure", _entity(m.get("Expression")), m.get("Property"), **base)
        if isinstance(node.get("Column"), dict):
            c = node["Column"]
            return BoundField("column", _entity(c.get("Expression")), c.get("Property"), **base)
        if isinstance(node.get("HierarchyLevel"), dict):
            h = node["HierarchyLevel"]
            hierarchy = (h.get("Expression") or {}).get("Hierarchy") if isinstance(h.get("Expression"), dict) else None
            if isinstance(hierarchy, dict):
                return BoundField("hierarchy", _entity(hierarchy.get("Expression")), hierarchy.get("Hierarchy"), level=h.get("Level"), **base)
    return BoundField("unsupported", **base)


def parse_literal(node: Any) -> Any:
    """A filter literal: `2024L` is 2024, `2.5D` is 2.5, `'text'` is text."""
    value = node.get("Literal", {}).get("Value") if isinstance(node, dict) else None
    if not isinstance(value, str):
        return None
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    try:
        if value.endswith("L"):
            return int(value[:-1])
        if value.endswith("D"):
            return float(value[:-1])
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _filters(config: Any, where: str) -> list[FilterRef]:
    out: list[FilterRef] = []
    for n, flt in enumerate((config or {}).get("filters", []) if isinstance(config, dict) else []):
        if not isinstance(flt, dict):
            continue
        f = parse_field(flt.get("field"), "filter", f"{where}.filters[{n}]")
        values: list[Any] = []
        try:
            for cond in flt.get("filter", {}).get("Where", []):
                for row in cond["Condition"]["In"]["Values"]:
                    values.append(parse_literal(row[0]))
        except (KeyError, TypeError, IndexError):
            pass
        is_column = f.kind == "column"
        out.append(FilterRef(f.table if is_column else None, f.name if is_column else None, values, f"{where}.filters[{n}]"))
    return out


def read_visual(path: Path, page: str, rel: str) -> VisualRead | None:
    data = _load(path)
    if data is None:
        return None
    body = data.get("visual") if isinstance(data.get("visual"), dict) else {}
    read = VisualRead(id=str(data.get("name", Path(path).parent.name)), page=page, visual_type=body.get("visualType"), file=rel)
    state = ((body.get("query") or {}).get("queryState")) or {}
    for role in sorted(state) if isinstance(state, dict) else []:
        for n, projection in enumerate((state[role] or {}).get("projections", [])):
            if isinstance(projection, dict):
                read.fields.append(parse_field(projection.get("field"), role, f"queryState.{role}.projections[{n}]", projection.get("active", True) is not False))
    read.filters = _filters(data.get("filterConfig"), "filterConfig")
    return read


def read_report(folder: Path) -> list[PageRead]:
    """Every page and visual of the folder, in folder-name order."""
    folder = Path(folder)
    pages: list[PageRead] = []
    root = folder / "definition" / "pages"
    for page_dir in sorted(p for p in root.glob("*") if p.is_dir()):
        rel = f"definition/pages/{page_dir.name}/page.json"
        data = _load(page_dir / "page.json") or {}
        page = PageRead(name=page_dir.name, display_name=data.get("displayName"), file=rel)
        page.filters = _filters(data.get("filterConfig"), "filterConfig")
        for visual_dir in sorted(p for p in (page_dir / "visuals").glob("*") if p.is_dir()):
            vrel = f"definition/pages/{page_dir.name}/visuals/{visual_dir.name}/visual.json"
            visual = read_visual(visual_dir / "visual.json", page_dir.name, vrel)
            if visual is not None:
                page.visuals.append(visual)
        pages.append(page)
    return pages
