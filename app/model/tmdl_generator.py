"""Serialize a ModelSpec into a TMDL project (T-502, FR-011, spec §9).

Deterministic and free of LLM calls: the same ModelSpec (and connection) gives byte-identical
files. Objects are sorted by name, files use LF line endings and tab indentation, and every
`lineageTag` is `uuid5(namespace, run_id + object path)`. Measures whose `dax` is null are left
out (T-503 fills them in).

Layout under `out_dir`:
    definition.pbism
    definition/database.tmdl
    definition/model.tmdl
    definition/relationships.tmdl
    definition/expressions.tmdl        # the SQL endpoint expression both storage modes use
    definition/tables/<Table>.tmdl
"""

import re
import uuid
from pathlib import Path

from app.contracts.model_plan import Measure, ModelSpec, ModelTable, Relationship
from app.workflow.run import SqlEndpoint

NAMESPACE = uuid.UUID("6f1d3a52-8c4e-4b7a-9d21-5e0a7c3b94f8")
COMPATIBILITY_LEVEL = 1604
EXPRESSION = "DatabaseQuery"
PBISM = '{\n  "version": "4.0",\n  "settings": {}\n}\n'
SIMPLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNSAFE_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TAB = "\t"


class ModelGenerationError(ValueError):
    """The ModelSpec can't be written as TMDL (a name or value TMDL can't hold, a missing table)."""


def _single_line(value: str, what: str) -> str:
    if "\n" in value or "\r" in value:
        raise ModelGenerationError(f"{what} can't contain a line break: {value!r}")
    return value


def quote(name: str, what: str = "name") -> str:
    """A TMDL object name: bare if it is a plain identifier, else in single quotes ('' for ')."""
    _single_line(name, what)
    if not name.strip():
        raise ModelGenerationError(f"The {what} is empty")
    if SIMPLE_NAME.fullmatch(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _column_ref(table: str, column: str) -> str:
    return f"{quote(table, 'table name')}.{quote(column, 'column name')}"


def _m_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _tag(spec: ModelSpec, path: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{spec.run_id}/{path}"))


def _doc(description: str | None, indent: int) -> list[str]:
    if not description:
        return []
    return [f"{TAB * indent}/// {line.strip()}" for line in description.splitlines() if line.strip()]


def _prop(indent: int, key: str, value: str | None) -> list[str]:
    if value is None or value == "":
        return []
    return [f"{TAB * indent}{key}: {_single_line(value, key)}"]


def _measure(spec: ModelSpec, m: Measure) -> list[str]:
    lines = _doc(m.description, 1)
    head = f"{TAB}measure {quote(m.name, 'measure name')}"
    dax = (m.dax or "").strip("\n")
    if "\n" in dax:
        lines.append(head + " =")
        lines += [f"{TAB * 3}{line}" if line.strip() else "" for line in dax.splitlines()]
    else:
        lines.append(f"{head} = {dax.strip()}")
    lines += _prop(2, "formatString", m.format_string)
    lines += _prop(2, "displayFolder", m.display_folder)
    lines.append(f"{TAB * 2}lineageTag: {_tag(spec, f'table/{m.table}/measure/{m.name}')}")
    return lines


def _column(spec: ModelSpec, table: ModelTable, c) -> list[str]:
    lines = [f"{TAB}column {quote(c.name, 'column name')}"]
    lines.append(f"{TAB * 2}dataType: {_single_line(c.data_type, 'data type')}")
    lines += _prop(2, "formatString", c.format_string)
    lines += _prop(2, "displayFolder", c.display_folder)
    if c.is_hidden:
        lines.append(f"{TAB * 2}isHidden")
    lines.append(f"{TAB * 2}lineageTag: {_tag(spec, f'table/{table.name}/column/{c.name}')}")
    lines.append(f"{TAB * 2}summarizeBy: none")
    lines.append(f"{TAB * 2}sourceColumn: {_single_line(c.source_column, 'source column')}")
    return lines


def _hierarchy(spec: ModelSpec, table: ModelTable, h) -> list[str]:
    path = f"table/{table.name}/hierarchy/{h.name}"
    lines = [f"{TAB}hierarchy {quote(h.name, 'hierarchy name')}", f"{TAB * 2}lineageTag: {_tag(spec, path)}"]
    for level in h.levels:  # level order is the drill order, so it is kept
        lines.append(f"{TAB * 2}level {quote(level.name, 'level name')}")
        lines.append(f"{TAB * 3}lineageTag: {_tag(spec, f'{path}/level/{level.name}')}")
        lines.append(f"{TAB * 3}column: {quote(level.column, 'column name')}")
    return lines


def _partition(spec: ModelSpec, table: ModelTable) -> list[str]:
    source = table.source
    if source is None:
        raise ModelGenerationError(f"Table {table.name!r} has no Lakehouse source (a calculated table can't be generated)")
    name = quote(table.name, "table name")
    if spec.storage_mode == "DirectLake":
        return [
            f"{TAB}partition {name} = entity",
            f"{TAB * 2}mode: directLake",
            f"{TAB * 2}source",
            f"{TAB * 3}entityName: {_single_line(source.table, 'source table')}",
            f"{TAB * 3}schemaName: {_single_line(source.schema_name, 'source schema')}",
            f"{TAB * 3}expressionSource: {EXPRESSION}",
        ]
    return [
        f"{TAB}partition {name} = m",
        f"{TAB * 2}mode: import",
        f"{TAB * 2}source =",
        f"{TAB * 4}let",
        f"{TAB * 5}Source = {EXPRESSION},",
        f"{TAB * 5}Data = Source{{[Schema={_m_string(source.schema_name)}, Item={_m_string(source.table)}]}}[Data]",
        f"{TAB * 4}in",
        f"{TAB * 5}Data",
    ]


def _table_file(spec: ModelSpec, table: ModelTable, measures: list[Measure]) -> str:
    lines = [f"table {quote(table.name, 'table name')}", f"{TAB}lineageTag: {_tag(spec, f'table/{table.name}')}"]
    blocks: list[list[str]] = []
    for m in sorted(measures, key=lambda m: m.name):
        blocks.append(_measure(spec, m))
    for c in sorted(table.columns, key=lambda c: c.name):
        blocks.append(_column(spec, table, c))
    for h in sorted(table.hierarchies, key=lambda h: h.name):
        blocks.append(_hierarchy(spec, table, h))
    blocks.append(_partition(spec, table))
    for block in blocks:
        lines.append("")
        lines += block
    return "\n".join(lines) + "\n"


def _relationships_file(spec: ModelSpec, tables: dict[str, ModelTable]) -> str:
    blocks: list[str] = []
    for r in sorted(spec.relationships, key=lambda r: r.name):
        for end in (r.from_, r.to):
            table = tables.get(end.table)
            if table is None or end.column not in {c.name for c in table.columns}:
                raise ModelGenerationError(f"Relationship {r.name!r} uses {end.table}.{end.column}, which is not in the model")
        lines = [f"relationship {_tag(spec, f'relationship/{r.name}')}"]
        lines += _relationship_props(r)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _relationship_props(r: Relationship) -> list[str]:
    lines = []
    if r.cardinality == "one_to_one":
        lines += [f"{TAB}fromCardinality: one", f"{TAB}toCardinality: one"]
    elif r.cardinality == "many_to_many":
        lines += [f"{TAB}fromCardinality: many", f"{TAB}toCardinality: many"]
    if r.cross_filter == "both":
        lines.append(f"{TAB}crossFilteringBehavior: bothDirections")
    lines.append(f"{TAB}fromColumn: {_column_ref(r.from_.table, r.from_.column)}")
    lines.append(f"{TAB}toColumn: {_column_ref(r.to.table, r.to.column)}")
    return lines


def _expressions_file(spec: ModelSpec, connection: SqlEndpoint) -> str:
    lines = [
        f"expression {EXPRESSION} =",
        f"{TAB * 2}let",
        f"{TAB * 3}database = Sql.Database({_m_string(connection.server)}, {_m_string(connection.database)})",
        f"{TAB * 2}in",
        f"{TAB * 3}database",
        f"{TAB}lineageTag: {_tag(spec, f'expression/{EXPRESSION}')}",
        f"{TAB}annotation PBI_IncludeFutureArtifacts = False",
    ]
    return "\n".join(lines) + "\n"


def _model_file(spec: ModelSpec, tables: list[ModelTable]) -> str:
    lines = [
        "model Model",
        f"{TAB}culture: en-US",
        f"{TAB}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{TAB}discourageImplicitMeasures",
        "",
    ]
    lines += [f"ref table {quote(t.name, 'table name')}" for t in tables]
    lines.append(f"ref expression {EXPRESSION}")
    return "\n".join(lines) + "\n"


def _database_file(spec: ModelSpec) -> str:
    return f"database {quote(spec.model_name, 'model name')}\n{TAB}compatibilityLevel: {COMPATIBILITY_LEVEL}\n"


def _file_name(table_name: str) -> str:
    cleaned = UNSAFE_FILE_CHARS.sub("_", table_name).strip(" .")
    if not cleaned:
        raise ModelGenerationError(f"Table name {table_name!r} can't be used as a file name")
    return cleaned + ".tmdl"


def render(spec: ModelSpec, connection: SqlEndpoint) -> dict[str, bytes]:
    """Every file of the project, keyed by its path relative to the project folder."""
    names = [t.name for t in spec.tables]
    if len(set(names)) != len(names):
        raise ModelGenerationError("Table names are not unique")
    tables = {t.name: t for t in spec.tables}
    ordered = sorted(spec.tables, key=lambda t: t.name)

    by_table: dict[str, list[Measure]] = {t.name: [] for t in spec.tables}
    for m in spec.measures:
        if m.dax is None:
            continue
        if m.table not in tables:
            raise ModelGenerationError(f"Measure {m.name!r} belongs to table {m.table!r}, which is not in the model")
        by_table[m.table].append(m)

    files: dict[str, str] = {
        "definition.pbism": PBISM,
        "definition/database.tmdl": _database_file(spec),
        "definition/model.tmdl": _model_file(spec, ordered),
        "definition/expressions.tmdl": _expressions_file(spec, connection),
    }
    relationships = _relationships_file(spec, tables)
    if relationships:
        files["definition/relationships.tmdl"] = relationships
    for t in ordered:
        path = f"definition/tables/{_file_name(t.name)}"
        if path in files:
            raise ModelGenerationError(f"Table {t.name!r} gives the same file name as another table")
        files[path] = _table_file(spec, t, by_table[t.name])
    return {path: text.encode("utf-8") for path, text in files.items()}


def generate_tmdl(spec: ModelSpec, out_dir: Path, connection: SqlEndpoint | None = None) -> list[Path]:
    """Write the TMDL project of `spec` into the empty or new folder `out_dir`.

    `connection` is the Lakehouse SQL endpoint the tables read from (the run's scope). Nothing is
    written if the spec can't be serialized, and an existing non-empty folder is never touched.
    """
    if connection is None:
        raise ModelGenerationError("The run's Lakehouse has no SQL endpoint to read the tables from")
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} already exists and is not empty")
    files = render(spec, connection)
    written = []
    for relative, data in sorted(files.items()):
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)  # bytes: Windows keeps LF
        written.append(path)
    return written
