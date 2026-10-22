"""The PBIR generator (T-602, FR-014): ReportPlan.json -> a PBIR report folder, bound by connection
to the approved semantic model in the sandbox.

Deterministic, no LLM: the same plan, ModelSpec and model item id give byte-identical files (fixed
key order, UTF-8, LF, two-space JSON). Page folders are `p<n>` in plan order and visual folders are
the plan's visual ids, so one visual can later be regenerated without touching the rest.

Layout of `out_dir`:
    definition.pbir                         byConnection -> the semantic model id
    definition/report.json, version.json
    definition/pages/pages.json
    definition/pages/<page>/page.json
    definition/pages/<page>/visuals/<visual id>/visual.json

Every file declares its PBIR `$schema`; `check_pbir` verifies what the schemas require of these files
(required keys, names, schema URLs) so a malformed folder is caught before anything is published.
The schema versions below are the ones published at
https://github.com/microsoft/json-schemas/tree/main/fabric/item/report (checked for T-602).
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.contracts.model_plan import ModelSpec
from app.contracts.report_plan import Filter, FieldRef, Interaction, Page, ReportPlan, Visual

BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report"
SCHEMAS = {
    "definition.pbir": f"{BASE}/definitionProperties/2.0.0/schema.json",
    "report.json": f"{BASE}/definition/report/3.1.0/schema.json",
    "version.json": f"{BASE}/definition/versionMetadata/1.0.0/schema.json",
    "pages.json": f"{BASE}/definition/pagesMetadata/1.0.0/schema.json",
    "page.json": f"{BASE}/definition/page/2.0.0/schema.json",
    "visual.json": f"{BASE}/definition/visualContainer/2.0.0/schema.json",
}
PBIR_VERSION = "4.0"  # definition.pbir `version`: 4.0 or higher allows the PBIR (definition/) format
DEFINITION_VERSION = "2.0.0"  # definition/version.json
BASE_THEME = {"name": "CY25SU12", "path": "BaseThemes/CY25SU12.json"}
PAGE_WIDTH, PAGE_HEIGHT, MARGIN = 1280, 720, 16
NAME = re.compile(r"^[\w-]{1,50}$")

# The query roles of each supported visual. The plan's roles map as: category -> Category / Rows,
# series -> Series / Columns, values -> Y / Values (a table or slicer has one Values role).
ROLES: dict[str, dict[str, str]] = {
    "card": {"values": "Values"},
    "lineChart": {"category": "Category", "series": "Series", "values": "Y"},
    "clusteredBarChart": {"category": "Category", "series": "Series", "values": "Y"},
    "clusteredColumnChart": {"category": "Category", "series": "Series", "values": "Y"},
    "tableEx": {"category": "Values", "values": "Values"},
    "pivotTable": {"category": "Rows", "series": "Columns", "values": "Values"},
    "slicer": {"category": "Values"},
}
DRILL_TYPES = ("lineChart", "clusteredBarChart", "clusteredColumnChart")
INTERACTIONS = {"filter": "DataFilter", "highlight": "HighlightFilter", "none": "NoFilter"}


class PbirGenerationError(ValueError):
    """The plan can't be turned into a report. Nothing is written."""


# --- fields -----------------------------------------------------------------------------------


def _source(table: str) -> dict:
    return {"Expression": {"SourceRef": {"Entity": table}}}


def _projection(field: dict, query_ref: str, active: bool | None = None) -> dict:
    out: dict[str, Any] = {"field": field, "queryRef": query_ref}
    if active is not None:
        out["active"] = active
    return out


def _projections(where: str, ref: FieldRef, spec: ModelSpec, drill: str | None = None) -> list[dict]:
    """The query projections of one plan field. A hierarchy becomes one projection per level."""
    if ref.measure is not None:
        measure = next((m for m in spec.measures if m.name == ref.measure), None)
        if measure is None:
            raise PbirGenerationError(f"{where}: measure '{ref.measure}' is not in the ModelSpec")
        return [_projection({"Measure": {**_source(measure.table), "Property": measure.name}}, f"{measure.table}.{measure.name}")]
    table = next((t for t in spec.tables if t.name == ref.table), None)
    if table is None:
        raise PbirGenerationError(f"{where}: table '{ref.table}' is not in the ModelSpec")
    if ref.column is not None:
        if ref.column not in {c.name for c in table.columns}:
            raise PbirGenerationError(f"{where}: column '{ref.table}'[{ref.column}] is not in the ModelSpec")
        return [_projection({"Column": {**_source(table.name), "Property": ref.column}}, f"{table.name}.{ref.column}")]
    hierarchy = next((h for h in table.hierarchies if h.name == ref.hierarchy), None)
    if hierarchy is None:
        raise PbirGenerationError(f"{where}: hierarchy '{ref.table}'.{ref.hierarchy} is not in the ModelSpec")
    out = []
    for n, level in enumerate(hierarchy.levels):
        field = {
            "HierarchyLevel": {
                "Expression": {"Hierarchy": {**_source(table.name), "Hierarchy": hierarchy.name}},
                "Level": level.name,
            }
        }
        # the first level is the one shown; the others are reached by drilling down
        out.append(_projection(field, f"{table.name}.{hierarchy.name}.{level.name}", active=(n == 0)))
    return out


def _literal(value: str | int | float) -> dict:
    if isinstance(value, bool):  # pydantic can coerce; a bool is never a filter value here
        raise PbirGenerationError("a filter value can't be a boolean")
    if isinstance(value, int):
        text = f"{value}L"
    elif isinstance(value, float):
        text = f"{value!r}D"
    else:
        text = "'" + value.replace("'", "''") + "'"
    return {"Literal": {"Value": text}}


def _filter(where: str, flt: Filter, spec: ModelSpec, name: str) -> dict:
    table = next((t for t in spec.tables if t.name == flt.table), None)
    if table is None or flt.column not in {c.name for c in table.columns}:
        raise PbirGenerationError(f"{where}: column '{flt.table}'[{flt.column}] is not in the ModelSpec")
    out: dict[str, Any] = {
        "name": name,
        "field": {"Column": {**_source(flt.table), "Property": flt.column}},
        "type": "Categorical",
    }
    if flt.values:
        out["filter"] = {
            "Version": 2,
            "From": [{"Name": "t", "Entity": flt.table, "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": flt.column}}],
                            "Values": [[_literal(v)] for v in flt.values],
                        }
                    }
                }
            ],
        }
    return out


def _filter_config(where: str, filters: list[Filter], spec: ModelSpec, prefix: str) -> dict | None:
    if not filters:
        return None
    return {"filters": [_filter(f"{where}.filters[{i}]", f, spec, f"{prefix}{i + 1}") for i, f in enumerate(filters)]}


# --- visuals and pages ------------------------------------------------------------------------


def _visual(visual: Visual, spec: ModelSpec, position: dict) -> dict:
    roles = ROLES.get(visual.type)
    if roles is None:
        raise PbirGenerationError(f"{visual.id}: visual type '{visual.type}' is not supported; allowed: {', '.join(ROLES)}")
    if not NAME.match(visual.id):
        raise PbirGenerationError(f"{visual.id}: visual ids must be 1-50 letters, digits, underscores or hyphens")
    drill = visual.drill
    if drill is not None:
        if visual.type not in DRILL_TYPES:
            raise PbirGenerationError(f"{visual.id}: drill isn't supported on a {visual.type}; use {', '.join(DRILL_TYPES)}")
        if not any(r.hierarchy == drill.hierarchy and drill.table in (None, r.table) for r in visual.fields.category):
            raise PbirGenerationError(f"{visual.id}: drill hierarchy '{drill.hierarchy}' must be one of the visual's category fields")

    state: dict[str, list[dict]] = {}
    for plan_role in ("category", "series", "values"):
        refs: list[FieldRef] = getattr(visual.fields, plan_role)
        if not refs:
            continue
        pbir_role = roles.get(plan_role)
        if pbir_role is None:
            raise PbirGenerationError(f"{visual.id}: a {visual.type} has no '{plan_role}' fields")
        for n, ref in enumerate(refs):
            state.setdefault(pbir_role, []).extend(_projections(f"{visual.id}.fields.{plan_role}[{n}]", ref, spec))
    if not state:
        raise PbirGenerationError(f"{visual.id}: the visual binds no field")
    if visual.type == "card" and "Values" not in state:
        raise PbirGenerationError(f"{visual.id}: a card needs a measure")
    if visual.type == "slicer" and "Values" not in state:
        raise PbirGenerationError(f"{visual.id}: a slicer needs a category field")

    body: dict[str, Any] = {
        "visualType": visual.type,
        "query": {"queryState": {role: {"projections": state[role]} for role in sorted(state)}},
        "visualContainerObjects": {
            "title": [{"properties": {"text": {"expr": {"Literal": {"Value": "'" + visual.title.replace("'", "''") + "'"}}}}}]
        },
        "drillFilterOtherVisuals": True,
    }
    out: dict[str, Any] = {"$schema": SCHEMAS["visual.json"], "name": visual.id, "position": position, "visual": body}
    config = _filter_config(visual.id, visual.filters, spec, "f")
    if config:
        out["filterConfig"] = config
    return out


def _positions(count: int) -> list[dict]:
    """A grid of equal cells, filled left to right: 1 column for 1 visual, 2 up to 4, else 3."""
    cols = 1 if count <= 1 else 2 if count <= 4 else 3
    rows = -(-count // cols)
    width = (PAGE_WIDTH - MARGIN * (cols + 1)) // cols
    height = (PAGE_HEIGHT - MARGIN * (rows + 1)) // rows
    return [
        {
            "x": MARGIN + (i % cols) * (width + MARGIN),
            "y": MARGIN + (i // cols) * (height + MARGIN),
            "z": i * 1000,
            "height": height,
            "width": width,
            "tabOrder": i * 1000,
        }
        for i in range(count)
    ]


def _page(page: Page, name: str, interactions: list[Interaction], spec: ModelSpec) -> dict:
    out: dict[str, Any] = {
        "$schema": SCHEMAS["page.json"],
        "name": name,
        "displayName": page.name,
        "displayOption": "FitToPage",
        "height": PAGE_HEIGHT,
        "width": PAGE_WIDTH,
    }
    config = _filter_config(f"pages[{page.name}]", page.filters, spec, "pf")
    if config:
        out["filterConfig"] = config
    ids = {v.id for v in page.visuals}
    mine = [i for i in interactions if i.from_visual in ids or i.to_visual in ids]
    for i in mine:
        if i.from_visual not in ids or i.to_visual not in ids:
            raise PbirGenerationError(f"interaction {i.from_visual} -> {i.to_visual}: both visuals must be on the same page")
        if i.type not in INTERACTIONS:
            raise PbirGenerationError(f"interaction {i.from_visual} -> {i.to_visual}: type '{i.type}' is not one of {', '.join(INTERACTIONS)}")
    if mine:
        out["visualInteractions"] = [{"source": i.from_visual, "target": i.to_visual, "type": INTERACTIONS[i.type]} for i in mine]
    return out


def _report() -> dict:
    return {
        "$schema": SCHEMAS["report.json"],
        "themeCollection": {
            "baseTheme": {
                "name": BASE_THEME["name"],
                "reportVersionAtImport": {"visual": "2.5.0", "report": "3.1.0", "page": "2.3.0"},
                "type": "SharedResources",
            }
        },
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [{"name": BASE_THEME["name"], "path": BASE_THEME["path"], "type": "BaseTheme"}],
            }
        ],
        "settings": {
            "useStylableVisualContainerHeader": True,
            "defaultFilterActionIsDataFilter": True,
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
        },
    }


def file_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")  # bytes: LF on Windows


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_bytes(data))


def build_files(plan: ReportPlan, model_item_id: str, spec: ModelSpec) -> dict[str, dict]:
    """Every file of the PBIR folder of `plan` as data, by relative path. Nothing is written; the
    repair service uses it to regenerate the file of one visual or page from the plan."""
    if not plan.pages:
        raise PbirGenerationError("The plan has no pages: nothing to generate")
    if not model_item_id.strip():
        raise PbirGenerationError("The semantic model's item id is required to bind the report")
    page_names = [f"p{n}" for n in range(1, len(plan.pages) + 1)]
    files: dict[str, dict] = {
        "definition.pbir": {
            "$schema": SCHEMAS["definition.pbir"],
            "version": PBIR_VERSION,
            "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={model_item_id.strip()}"}},
        },
        "definition/report.json": _report(),
        "definition/version.json": {"$schema": SCHEMAS["version.json"], "version": DEFINITION_VERSION},
        "definition/pages/pages.json": {"$schema": SCHEMAS["pages.json"], "pageOrder": page_names, "activePageName": page_names[0]},
    }
    seen: set[str] = set()
    for name, page in zip(page_names, plan.pages):
        if not page.visuals:
            raise PbirGenerationError(f"pages[{page.name}]: the page has no visuals")
        files[f"definition/pages/{name}/page.json"] = _page(page, name, plan.interactions, spec)
        for visual, position in zip(page.visuals, _positions(len(page.visuals))):
            if visual.id in seen:
                raise PbirGenerationError(f"visual id '{visual.id}' is used more than once")
            seen.add(visual.id)
            files[f"definition/pages/{name}/visuals/{visual.id}/visual.json"] = _visual(visual, spec, position)
    for i in plan.interactions:
        if i.from_visual not in seen or i.to_visual not in seen:
            raise PbirGenerationError(f"interaction {i.from_visual} -> {i.to_visual}: a visual doesn't exist")
    return files


def generate_pbir(plan: ReportPlan, model_item_id: str, out_dir: Path, spec: ModelSpec) -> list[Path]:
    """Write the PBIR folder of `plan` into `out_dir` (created if needed) and return the files, sorted.

    `spec` is the approved ModelSpec: it says which table owns each measure. Raises
    PbirGenerationError, naming the visual, before the first file is written.
    """
    files = build_files(plan, model_item_id, spec)
    out_dir = Path(out_dir)
    written = []
    for rel in sorted(files):
        _write(out_dir / rel, files[rel])
        written.append(out_dir / rel)
    return written


def folder_digest(folder: Path) -> str:
    """One hash over the path and bytes of every file of the report except `renders/` (the PNGs
    that validation saves beside the definition for review), so rendering doesn't change it."""
    folder = Path(folder)
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.relative_to(folder).parts[0] != "renders"):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


# --- the structural check --------------------------------------------------------------------------


def check_pbir(folder: Path) -> list[str]:
    """What the PBIR schemas require of the files `generate_pbir` writes. Returns the problems (empty
    if the folder is well formed): unreadable JSON, a wrong `$schema`, missing required keys, bad
    names, and page or visual names that aren't unique."""
    folder = Path(folder)
    problems: list[str] = []

    def load(rel: str) -> dict | None:
        path = folder / rel
        if not path.is_file():
            problems.append(f"{rel}: file is missing")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{rel}: not valid JSON ({exc})")
            return None
        if not isinstance(data, dict):
            problems.append(f"{rel}: not a JSON object")
            return None
        return data

    def need(rel: str, data: dict, kind: str, keys: tuple[str, ...]) -> None:
        if data.get("$schema") != SCHEMAS[kind]:
            problems.append(f"{rel}: $schema must be {SCHEMAS[kind]}")
        for key in keys:
            if key not in data:
                problems.append(f"{rel}: required property '{key}' is missing")

    pbir = load("definition.pbir")
    if pbir is not None:
        need("definition.pbir", pbir, "definition.pbir", ("version", "datasetReference"))
        if not str((pbir.get("datasetReference", {}).get("byConnection", {}) or {}).get("connectionString", "")).startswith("semanticmodelid="):
            problems.append("definition.pbir: datasetReference.byConnection.connectionString must be 'semanticmodelid=<id>'")
    for rel, kind, keys in (
        ("definition/report.json", "report.json", ()),
        ("definition/version.json", "version.json", ("version",)),
    ):
        data = load(rel)
        if data is not None:
            need(rel, data, kind, keys)
    pages = load("definition/pages/pages.json")
    order = []
    if pages is not None:
        need("definition/pages/pages.json", pages, "pages.json", ())
        order = pages.get("pageOrder", [])
    page_dirs = sorted(p.name for p in (folder / "definition" / "pages").glob("*") if p.is_dir())
    if sorted(order) != page_dirs:
        problems.append(f"definition/pages/pages.json: pageOrder {order} doesn't match the page folders {page_dirs}")
    visual_names: dict[str, str] = {}
    for page_name in page_dirs:
        rel = f"definition/pages/{page_name}/page.json"
        page = load(rel)
        if page is not None:
            need(rel, page, "page.json", ("name", "displayName", "displayOption", "width", "height"))
            if page.get("name") != page_name or not NAME.match(page_name):
                problems.append(f"{rel}: name must equal the folder name '{page_name}' (1-50 letters, digits, underscores or hyphens)")
        for vdir in sorted(p for p in (folder / "definition" / "pages" / page_name / "visuals").glob("*") if p.is_dir()):
            vrel = f"definition/pages/{page_name}/visuals/{vdir.name}/visual.json"
            visual = load(vrel)
            if visual is None:
                continue
            need(vrel, visual, "visual.json", ("name", "position"))
            if visual.get("name") != vdir.name or not NAME.match(vdir.name):
                problems.append(f"{vrel}: name must equal the folder name '{vdir.name}'")
            if vdir.name in visual_names:
                problems.append(f"{vrel}: visual name '{vdir.name}' is also used on page {visual_names[vdir.name]}")
            visual_names[vdir.name] = page_name
            position = visual.get("position", {})
            for key in ("x", "y", "width", "height"):
                if not isinstance(position.get(key), (int, float)):
                    problems.append(f"{vrel}: position.{key} must be a number")
            if ("visual" in visual) == ("visualGroup" in visual):
                problems.append(f"{vrel}: exactly one of 'visual' or 'visualGroup' is required")
    return problems
