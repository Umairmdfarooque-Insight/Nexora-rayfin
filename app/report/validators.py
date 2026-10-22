"""The Gate G5 checks (T-603, FR-015, spec §12): schema, binding, query and render of a report.

Deterministic code, no model. G5.SCHEMA and G5.BIND read the PBIR files as they are on disk;
G5.QUERY runs each visual's equivalent DAX query against the deployed model through
`executeQueries`; G5.RENDER exports each page to PNG for human review. Each check carries
`evidence.defect`: "report" when the report is at fault (the PBIR is invalid, or it binds an object
the approved ModelSpec doesn't have) and "model" when the object is in the ModelSpec but its query
fails. The stage decides what a failure means for the run.
"""

from pathlib import Path
from typing import Any

from app.connectors.fabric_api import FabricTimeout
from app.connectors.powerbi_api import DaxError, ExportUnavailable, PowerBiClient
from app.contracts.model_plan import ModelSpec
from app.contracts.validation_report import ValidationCheck
from app.model.validators import check, skipped
from app.report.pbir_generator import check_pbir
from app.report.pbir_reader import BoundField, FilterRef, PageRead, VisualRead

MAX_QUERY_ROWS = 100
RENDERS = "renders"


# --- objects -----------------------------------------------------------------------------------


def object_of(rel: str) -> str:
    """The repair scope of a PBIR file: `visual:<id>`, `page:<name>` or `report`."""
    parts = rel.split("/")
    if len(parts) >= 6 and parts[:3] == ["definition", "pages", parts[2]] and parts[3] == "visuals":
        return f"visual:{parts[4]}"
    if len(parts) == 4 and parts[:2] == ["definition", "pages"] and parts[3] == "page.json":
        return f"page:{parts[2]}"
    return "report"


def files_of(obj: str, pages: list[PageRead]) -> set[str]:
    """The files a repair of `obj` may change."""
    kind, _, name = obj.partition(":")
    if kind == "visual":
        return {v.file for p in pages for v in p.visuals if v.id == name}
    if kind == "page":
        return {p.file for p in pages if p.name == name}
    return set()


# --- G5.SCHEMA -----------------------------------------------------------------------------------


def check_schema(folder: Path) -> list[ValidationCheck]:
    problems = check_pbir(folder)
    if not problems:
        return [check("G5.SCHEMA", "report", "pass", "Every PBIR file has its schema and required properties", None, {"defect": None})]
    out = []
    for problem in problems:
        rel = problem.split(":", 1)[0]
        out.append(check("G5.SCHEMA", object_of(rel), "fail", problem, "SCHEMA_INVALID", {"defect": "report", "file": rel}))
    return out


# --- G5.BIND ---------------------------------------------------------------------------------------


def field_problem(f: BoundField, spec: ModelSpec) -> str | None:
    """Why `f` isn't an object of the approved ModelSpec, or None."""
    if f.kind == "unsupported":
        return f"{f.label()} can't be checked against the model"
    if f.kind == "measure":
        measure = next((m for m in spec.measures if m.name == f.name), None)
        if measure is None:
            return f"{f.label()} is not in the approved model"
        if f.table != measure.table:
            return f"{f.label()} is bound to table '{f.table}' but belongs to '{measure.table}'"
        if not measure.dax:
            return f"{f.label()} has no approved DAX"
        return None
    table = next((t for t in spec.tables if t.name == f.table), None)
    if table is None:
        return f"table '{f.table}' of {f.label()} is not in the approved model"
    if f.kind == "column":
        return None if f.name in {c.name for c in table.columns} else f"{f.label()} is not in the approved model"
    hierarchy = next((h for h in table.hierarchies if h.name == f.name), None)
    if hierarchy is None or f.level not in {lv.name for lv in hierarchy.levels}:
        return f"{f.label()} is not in the approved model"
    return None


def filter_problem(flt: FilterRef, spec: ModelSpec) -> str | None:
    if flt.table is None or flt.column is None:
        return f"the filter at {flt.path} doesn't name a column"
    table = next((t for t in spec.tables if t.name == flt.table), None)
    if table is None or flt.column not in {c.name for c in table.columns}:
        return f"filter column '{flt.table}'[{flt.column}] is not in the approved model"
    return None


def check_binding(pages: list[PageRead], spec: ModelSpec) -> list[ValidationCheck]:
    out: list[ValidationCheck] = []
    for page in pages:
        problems = [p for p in (filter_problem(f, spec) for f in page.filters) if p]
        for problem in problems:
            out.append(check("G5.BIND", f"page:{page.name}", "fail", f"{page.name}: {problem}", "BINDING_MISSING", {"defect": "report", "file": page.file}))
        for v in page.visuals:
            found = [f"{v.id}: {p}" for f in v.fields if (p := field_problem(f, spec))]
            found += [f"{v.id}: {p}" for f in v.filters if (p := filter_problem(f, spec))]
            if not v.fields:
                found.append(f"{v.id}: the visual binds no field")
            obj = f"visual:{v.id}"
            if found:
                out += [check("G5.BIND", obj, "fail", m, "BINDING_MISSING", {"defect": "report", "file": v.file}) for m in found]
            else:
                out.append(check("G5.BIND", obj, "pass", f"{v.id}: all {len(v.fields)} bound field(s) exist in the approved model", None, {"defect": None, "fields": [f.label() for f in v.fields]}))
    return out


# --- G5.QUERY --------------------------------------------------------------------------------------


def _table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _column(table: str, column: str) -> str:
    return f"{_table(table)}[{column.replace(']', ']]')}]"


def _literal(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return "BLANK()"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace('"', '""') + '"'


def _group_column(f: BoundField, spec: ModelSpec) -> str | None:
    if f.kind == "column":
        return _column(f.table or "", f.name or "")
    if f.kind == "hierarchy":
        table = next((t for t in spec.tables if t.name == f.table), None)
        hierarchy = next((h for h in (table.hierarchies if table else []) if h.name == f.name), None)
        level = next((lv for lv in (hierarchy.levels if hierarchy else []) if lv.name == f.level), None)
        return _column(f.table or "", level.column) if level and f.active else None
    return None


def build_query(visual: VisualRead, page: PageRead, spec: ModelSpec) -> str:
    """The DAX that gives the visual its rows: its group-by columns (a hierarchy contributes its
    shown level), its filters (page and visual) and its measures."""
    groups = list(dict.fromkeys(g for f in visual.fields if (g := _group_column(f, spec))))
    measures = list(dict.fromkeys(f.name for f in visual.fields if f.kind == "measure" and f.name))
    filters = [
        f"TREATAS({{{', '.join(_literal(v) for v in flt.values)}}}, {_column(flt.table, flt.column)})"
        for flt in [*page.filters, *visual.filters]
        if flt.table and flt.column and flt.values
    ]
    args = [*groups, *filters]
    for name in measures:
        args += ['"' + name.replace('"', '""') + '"', "[" + name.replace("]", "]]") + "]"]
    table = f"SUMMARIZECOLUMNS({', '.join(args)})"
    return f"EVALUATE\n{table}" if not groups else f"EVALUATE\nTOPN({MAX_QUERY_ROWS}, {table})"


def check_query(
    pages: list[PageRead], spec: ModelSpec, pbi: PowerBiClient, workspace_id: str, dataset_id: str, unbound: set[str]
) -> list[ValidationCheck]:
    out: list[ValidationCheck] = []
    for page in pages:
        for v in page.visuals:
            obj = f"visual:{v.id}"
            if obj in unbound:
                out.append(skipped("G5.QUERY", obj, "its binding failed"))
                continue
            dax = build_query(v, page, spec)
            try:
                rows = pbi.execute_dax(workspace_id, dataset_id, dax, impersonate=None)
            except DaxError as exc:
                out.append(check("G5.QUERY", obj, "fail", f"{v.id}: the query failed: {exc}", "QUERY_FAILED", {"defect": "model", "query": dax, "response": str(exc), "file": v.file}))
                continue
            if rows:
                out.append(check("G5.QUERY", obj, "pass", f"{v.id}: the query returned {len(rows)} row(s)", None, {"defect": None, "query": dax, "rows": len(rows)}))
            else:
                out.append(check("G5.QUERY", obj, "warning", f"{v.id}: no data: the query returned no rows", "NO_DATA", {"defect": None, "query": dax, "rows": 0}))
    return out


# --- G5.RENDER -------------------------------------------------------------------------------------


def check_render(pages: list[PageRead], folder: Path, pbi: PowerBiClient, workspace_id: str, report_id: str) -> list[ValidationCheck]:
    """Export each page to PNG into `<folder>/renders/<page>.png`. Never a failure: if the export
    isn't available or doesn't finish the check is a warning, and the other checks decide."""
    out: list[ValidationCheck] = []
    renders = Path(folder) / RENDERS
    for n, page in enumerate(pages):
        obj = f"page:{page.name}"
        try:
            png = pbi.export_report_page(workspace_id, report_id, page.name)
        except ExportUnavailable as exc:
            out.append(check("G5.RENDER", obj, "warning", f"export unavailable: {exc}", "RENDER_UNAVAILABLE", {"defect": None}))
            out += [check("G5.RENDER", f"page:{p.name}", "warning", "export unavailable: not tried after the first page failed", "RENDER_UNAVAILABLE", {"defect": None}) for p in pages[n + 1 :]]
            return out
        except FabricTimeout as exc:
            out.append(check("G5.RENDER", obj, "warning", f"export timed out: {exc}", "RENDER_TIMEOUT", {"defect": None}))
            continue
        renders.mkdir(parents=True, exist_ok=True)
        (renders / f"{page.name}.png").write_bytes(png)
        out.append(check("G5.RENDER", obj, "pass", f"page {page.name} exported to {RENDERS}/{page.name}.png", None, {"defect": None, "file": f"{RENDERS}/{page.name}.png", "bytes": len(png)}))
    return out
