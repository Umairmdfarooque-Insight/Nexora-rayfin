"""Optional column-definitions upload (table, column, definition) on the New Run form.

Wholly optional: a run with none works exactly as before. Uploaded any time, in any state,
and merged into MetadataInventory's table/column descriptions the next time the run is
harvested (app.workflow.harvest_stage). A row that doesn't match any harvested table or
column is a harvest warning, never an upload or harvest failure (it may simply not have run
yet, or the name may be wrong -- both are the uploader's to fix, not a reason to block).
"""

import csv
import io
import re
from pathlib import Path

from app.contracts.column_definitions import SCHEMA_VERSION, ColumnDefinitionEntry, ColumnDefinitions
from app.store.base import RunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, utc_now

ARTIFACT = "ColumnDefinitions"
CURRENT = "ColumnDefinitions.json"
_VERSION_FILE = re.compile(r"ColumnDefinitions\.v(\d+)\.json$")

_REQUIRED_HEADERS = {
    "table": {"table", "table_name", "tablename"},
    "column": {"column", "column_name", "columnname"},
    "definition": {"definition", "description", "meaning", "notes"},
}
_OPTIONAL_HEADERS = {"schema": {"schema", "schema_name", "schemaname"}}


class InvalidColumnDefinitionsFile(Exception):
    """The uploaded file has no usable header or no usable rows (400 INVALID_FILE)."""


def _match_header(fieldnames: list[str]) -> dict[str, str]:
    normalized = {(f or "").strip().lower(): f for f in fieldnames}
    found: dict[str, str] = {}
    for key, aliases in {**_REQUIRED_HEADERS, **_OPTIONAL_HEADERS}.items():
        for alias in aliases:
            if alias in normalized:
                found[key] = normalized[alias]
                break
    missing = _REQUIRED_HEADERS.keys() - found.keys()
    if missing:
        raise InvalidColumnDefinitionsFile(
            f"The file has no column for: {', '.join(sorted(missing))}. "
            "Expected headers like table, column, definition (schema is optional)."
        )
    return found


def _parse_csv(content: bytes) -> tuple[list[dict], dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidColumnDefinitionsFile("The CSV file isn't valid UTF-8 text") from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise InvalidColumnDefinitionsFile("The file has no header row")
    return list(reader), _match_header(list(reader.fieldnames))


def _parse_xlsx(content: bytes) -> tuple[list[dict], dict[str, str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise InvalidColumnDefinitionsFile("Reading .xlsx files needs the openpyxl package") from None
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several distinct error types for a bad file
        raise InvalidColumnDefinitionsFile(f"The Excel file could not be read ({type(exc).__name__})") from None
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        raise InvalidColumnDefinitionsFile("The file has no header row")
    fieldnames = [str(h) if h is not None else "" for h in header]
    columns = _match_header(fieldnames)
    raw_rows = [dict(zip(fieldnames, row)) for row in rows]
    return raw_rows, columns


def parse_entries(filename: str, content: bytes) -> list[ColumnDefinitionEntry]:
    """Raises InvalidColumnDefinitionsFile for an unusable file. A row missing a table,
    column or definition is skipped, not an error -- only an empty result fails."""
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        rows, columns = _parse_xlsx(content)
    elif lower.endswith(".csv"):
        rows, columns = _parse_csv(content)
    else:
        raise InvalidColumnDefinitionsFile("Only .csv and .xlsx files are supported")

    entries: list[ColumnDefinitionEntry] = []
    for row in rows:
        table = str(row.get(columns["table"]) or "").strip()
        column = str(row.get(columns["column"]) or "").strip()
        definition = str(row.get(columns["definition"]) or "").strip()
        schema = str(row.get(columns.get("schema", "")) or "").strip() if "schema" in columns else ""
        if not schema and "." in table:
            schema, _, table = table.rpartition(".")
        if not table or not column or not definition:
            continue
        entries.append(ColumnDefinitionEntry(schema=schema, table=table, column=column, definition=definition))

    if not entries:
        raise InvalidColumnDefinitionsFile(
            "The file has no usable rows (each needs a table, column and definition)"
        )
    return entries


def _next_version(run_dir: Path, previous_version: int) -> int:
    existing = [
        int(m.group(1))
        for p in (run_dir / "column_definitions").glob("ColumnDefinitions.v*.json")
        if (m := _VERSION_FILE.search(p.name))
    ]
    return max([0, *existing, previous_version]) + 1


def save_column_definitions(store: RunStore, runs_root: Path, run_id: str, filename: str, entries, actor: str):
    """Writes the next ColumnDefinitions version and registers the artifact. Any run state
    is allowed; nothing else about the run changes."""
    run = store.get(run_id)
    run_dir = Path(runs_root) / run_id
    previous = run.artifacts.get(ARTIFACT)
    version = _next_version(run_dir, previous.version if previous else 0)

    result = ColumnDefinitions(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        version=version,
        uploaded_at=utc_now(),
        filename=filename,
        entries=entries,
    )
    data = result.to_dict()
    write_json(run_dir / "column_definitions" / f"ColumnDefinitions.v{version}.json", data)
    write_json(run_dir / CURRENT, data)
    run.artifacts[ARTIFACT] = ArtifactRef(
        path=CURRENT, version=version, hash=canonical_hash(data), status="draft", updated_at=utc_now()
    )
    store.save(run)
    return store.get(run_id), result


def load_column_definitions(runs_root: Path, run_id: str) -> ColumnDefinitions | None:
    """None when nothing has been uploaded for this run yet (not an error -- the feature is
    optional)."""
    path = Path(runs_root) / run_id / CURRENT
    if not path.is_file():
        return None
    return ColumnDefinitions.model_validate_json(path.read_text(encoding="utf-8"))


def apply_to_tables(definitions: ColumnDefinitions | None, tables: list[dict]) -> list[str]:
    """Overlays each entry's definition onto the matching harvested table/column
    description, in place. An entry with a blank schema matches any schema with that table
    name. Returns one warning string per entry that matched nothing; never raises."""
    if definitions is None or not definitions.entries:
        return []

    by_key: dict[tuple[str, str, str], list[ColumnDefinitionEntry]] = {}
    for entry in definitions.entries:
        key = (entry.schema_name.lower(), entry.table_name.lower(), entry.column_name.lower())
        by_key.setdefault(key, []).append(entry)

    warnings: list[str] = []
    matched_ids = set()
    for table in tables:
        schema_name = str(table.get("schema", "")).lower()
        table_name = str(table.get("table_name", "")).lower()
        for column in table.get("columns") or []:
            column_name = str(column.get("name", "")).lower()
            candidates = by_key.get((schema_name, table_name, column_name)) or by_key.get(("", table_name, column_name))
            if candidates:
                column["description"] = candidates[0].definition
                matched_ids.add(id(candidates[0]))

    for entry in definitions.entries:
        if id(entry) not in matched_ids:
            qualified = f"{entry.schema_name}.{entry.table_name}" if entry.schema_name else entry.table_name
            warnings.append(f"Column definition for {qualified}.{entry.column_name} has no matching table/column")

    return warnings
