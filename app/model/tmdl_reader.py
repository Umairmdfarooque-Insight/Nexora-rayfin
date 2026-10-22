"""A small reader for the TMDL folder that is deployed (T-504).

Gate G4 checks what is actually deployed, so it reads the files rather than trusting the
ModelSpec. It understands the shapes `tmdl_generator` writes (tables with columns, measures,
hierarchies and partitions; relationships; roles) and tolerates hand edits of them. It is not a
full TMDL parser.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

ITEM = re.compile(r'Item\s*=\s*"((?:[^"]|"")*)"')
SCHEMA = re.compile(r'Schema\s*=\s*"((?:[^"]|"")*)"')


@dataclass
class TmdlColumn:
    name: str
    source_column: str | None = None
    data_type: str | None = None


@dataclass
class TmdlTable:
    name: str
    columns: dict[str, TmdlColumn] = field(default_factory=dict)
    measures: dict[str, str] = field(default_factory=dict)  # name -> DAX
    hierarchies: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # name -> [(level, column)]
    source: tuple[str, str] | None = None  # (schema, table) of the partition


@dataclass
class TmdlRelationship:
    id: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_cardinality: str = "many"
    to_cardinality: str = "one"


@dataclass
class TmdlRole:
    name: str
    filters: dict[str, str] = field(default_factory=dict)  # table -> filter expression


@dataclass
class TmdlModel:
    tables: dict[str, TmdlTable] = field(default_factory=dict)
    relationships: list[TmdlRelationship] = field(default_factory=list)
    roles: dict[str, TmdlRole] = field(default_factory=dict)


def read_name(text: str) -> tuple[str, str]:
    """The object name at the start of `text` (quoted or plain) and what follows it."""
    text = text.lstrip()
    if text.startswith("'"):
        out, i = [], 1
        while i < len(text):
            if text[i] == "'":
                if text[i + 1 : i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1 :]
            out.append(text[i])
            i += 1
        return "".join(out), ""
    m = re.match(r"[^\s=.:]+", text)
    return (m.group(0), text[m.end() :]) if m else ("", text)


def read_column_ref(text: str) -> tuple[str, str]:
    table, rest = read_name(text)
    rest = rest.lstrip()
    if rest.startswith("."):
        column, _ = read_name(rest[1:])
        return table, column
    return table, ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _block(lines: list[str], start: int, depth: int) -> tuple[list[str], int]:
    """The lines after `start` that are indented deeper than `depth` (blank lines included)."""
    out, i = [], start + 1
    while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > depth):
        out.append(lines[i])
        i += 1
    while out and not out[-1].strip():
        out.pop()
    return out, i


def _expression(first: str, body: list[str]) -> str:
    """A `name = expr` object's expression: inline, or the deeper lines that follow it."""
    inline = first.strip()
    if inline:
        return inline
    return "\n".join(line.strip("\t") for line in body if _indent(line) >= 3 or not line.strip()).strip()


def parse_table(text: str) -> TmdlTable | None:
    lines = text.replace("\r\n", "\n").split("\n")
    table = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("///"):
            i += 1
            continue
        if _indent(line) == 0 and stripped.startswith("table "):
            table = TmdlTable(name=read_name(stripped[6:])[0])
            i += 1
            continue
        if table is None or _indent(line) != 1:
            i += 1
            continue
        body, nxt = _block(lines, i, 1)
        kind, _, rest = stripped.partition(" ")
        if kind == "measure":
            name, after = read_name(rest)
            after = after.lstrip()
            if after.startswith("="):
                table.measures[name] = _expression(after[1:], [b for b in body if _indent(b) >= 3 or not b.strip()])
        elif kind == "column":
            name, _ = read_name(rest)
            col = TmdlColumn(name=name)
            for b in body:
                key, _, value = b.strip().partition(":")
                if key == "sourceColumn":
                    col.source_column = value.strip()
                elif key == "dataType":
                    col.data_type = value.strip()
            table.columns[name] = col
        elif kind == "hierarchy":
            name, _ = read_name(rest)
            levels, current = [], None
            for b in body:
                bs = b.strip()
                if bs.startswith("level "):
                    current = read_name(bs[6:])[0]
                elif bs.startswith("column:") and current is not None:
                    levels.append((current, read_name(bs[7:])[0]))
            table.hierarchies[name] = levels
        elif kind == "partition":
            joined = "\n".join(body)
            entity = re.search(r"entityName:\s*(.+)", joined)
            schema = re.search(r"schemaName:\s*(.+)", joined)
            if entity and schema:
                table.source = (schema.group(1).strip(), entity.group(1).strip())
            else:
                item, sch = ITEM.search(joined), SCHEMA.search(joined)
                if item and sch:
                    table.source = (sch.group(1).replace('""', '"'), item.group(1).replace('""', '"'))
        i = nxt
    return table


def parse_relationships(text: str) -> list[TmdlRelationship]:
    out: list[TmdlRelationship] = []
    current: dict | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _indent(line) == 0 and stripped.startswith("relationship "):
            if current:
                out.append(_relationship(current))
            current = {"id": stripped[13:].strip()}
        elif current is not None and stripped and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = value.strip()
    if current:
        out.append(_relationship(current))
    return out


def _relationship(props: dict) -> TmdlRelationship:
    from_table, from_column = read_column_ref(props.get("fromColumn", ""))
    to_table, to_column = read_column_ref(props.get("toColumn", ""))
    return TmdlRelationship(
        id=props["id"],
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        from_cardinality=props.get("fromCardinality", "many"),
        to_cardinality=props.get("toCardinality", "one"),
    )


def parse_role(text: str) -> TmdlRole | None:
    lines = text.replace("\r\n", "\n").split("\n")
    role = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _indent(line) == 0 and stripped.startswith("role "):
            role = TmdlRole(name=read_name(stripped[5:])[0])
        elif role is not None and _indent(line) == 1 and stripped.startswith("tablePermission "):
            name, after = read_name(stripped[len("tablePermission ") :])
            after = after.lstrip()
            if after.startswith("="):
                body, _ = _block(lines, i, 1)
                role.filters[name] = _expression(after[1:], body)
    return role


def read_model(folder: Path) -> TmdlModel:
    """The tables, relationships and roles of a TMDL project folder."""
    definition = Path(folder) / "definition"
    model = TmdlModel()
    for path in sorted((definition / "tables").glob("*.tmdl")):
        table = parse_table(path.read_text(encoding="utf-8"))
        if table is not None:
            model.tables[table.name] = table
    rel = definition / "relationships.tmdl"
    if rel.is_file():
        model.relationships = parse_relationships(rel.read_text(encoding="utf-8"))
    for path in sorted((definition / "roles").glob("*.tmdl")):
        role = parse_role(path.read_text(encoding="utf-8"))
        if role is not None:
            model.roles[role.name] = role
    return model
