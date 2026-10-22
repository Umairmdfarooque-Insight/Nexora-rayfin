"""Measured evidence for candidate relationships (T-303, FR-007, business rule 7).

Metadata is the source of truth: a candidate that names a table or column outside the
inventory is rejected, whoever proposed it. Evidence queries are read-only and
aggregate-only, and identifiers are bracket-quoted from the inventory. Single-column keys only
(composite keys are out of scope).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT
from app.contracts.metadata_inventory import (
    ColumnRef,
    InventoryColumn,
    InventoryTable,
    MetadataInventory,
    RelationshipEvidence,
    RelationshipQueryEvidence,
    TableProfile,
)
from app.discovery.data_profiler import QUERY_TIMEOUT_SECONDS, classify_error, qualified, quote_identifier
from app.discovery.relationship_candidates import Candidate, table_key
from app.workflow.run import utc_now

THRESHOLDS_PATH = PROJECT_ROOT / "config" / "relationship_thresholds.yaml"

_INTEGER = {"BYTE", "TINYINT", "SHORT", "SMALLINT", "INT", "INTEGER", "LONG", "BIGINT"}
_DECIMAL = {"FLOAT", "REAL", "DOUBLE", "DECIMAL", "DEC", "NUMERIC"}
_STRING = {"STRING", "VARCHAR", "CHAR", "NVARCHAR", "NCHAR", "TEXT"}
_DATE = {"DATE", "TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "DATETIME", "DATETIME2", "SMALLDATETIME"}
_BOOLEAN = {"BOOLEAN", "BIT"}


@dataclass(frozen=True)
class Thresholds:
    high_orphan_max: float
    high_overlap_min: float
    medium_orphan_max: float
    require_unique_target: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "high": {"orphan_rate_max": self.high_orphan_max, "overlap_pct_min": self.high_overlap_min},
            "medium": {"orphan_rate_max": self.medium_orphan_max},
            "require_unique_target": self.require_unique_target,
        }


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Thresholds:
    label = path.name
    from_fabric = path is THRESHOLDS_PATH and config_files.from_fabric()
    try:
        text = config_files.read_text(label) if from_fabric else path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file {label} is missing ({path})") from None
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"Configuration file {label} can't be read: {exc}") from None
    except Exception as exc:  # the OneLake connector: a missing file, a network error, …
        if not from_fabric:
            raise
        raise ConfigError(f"Configuration file {label} is missing or can't be read from Files/config/: {exc}") from None

    def number(section: str, key: str) -> float:
        value = (data.get(section) or {}).get(key) if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ConfigError(f"Configuration file {label} is invalid: {section}.{key} must be a number from 0 to 100")
        return float(value)

    unique = data.get("require_unique_target", True) if isinstance(data, dict) else True
    if not isinstance(unique, bool):
        raise ConfigError(f"Configuration file {label} is invalid: require_unique_target must be true or false")
    return Thresholds(
        high_orphan_max=number("high", "orphan_rate_max"),
        high_overlap_min=number("high", "overlap_pct_min"),
        medium_orphan_max=number("medium", "orphan_rate_max"),
        require_unique_target=unique,
    )


def type_family(data_type: str | None) -> str:
    base = (data_type or "").upper()
    for separator in ("(", "<", " "):
        base = base.split(separator)[0]
    for family, names in (
        ("integer", _INTEGER), ("decimal", _DECIMAL), ("string", _STRING), ("date", _DATE), ("boolean", _BOOLEAN)
    ):
        if base in names:
            return family
    return f"other:{base}"


def types_compatible(a: str | None, b: str | None) -> bool:
    """Same family, or integer with decimal. Unknown types only match themselves."""
    fa, fb = type_family(a), type_family(b)
    if fa == fb:
        return bool(a or b)
    return {fa, fb} == {"integer", "decimal"}


def _percent(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 2)


def _find_table(inventory: MetadataInventory, name: str) -> InventoryTable | None:
    """Exact "schema.table" (any case), else a bare table name that is unique in the inventory."""
    lowered = name.strip().lower()
    for table in inventory.tables:
        if table_key(table.schema_name, table.table_name).lower() == lowered:
            return table
    bare = [t for t in inventory.tables if t.table_name.lower() == lowered]
    return bare[0] if len(bare) == 1 else None


def _find_column(table: InventoryTable, name: str) -> InventoryColumn | None:
    for column in table.columns:
        if column.name == name:
            return column
    lowered = name.lower()
    return next((c for c in table.columns if c.name.lower() == lowered), None)


def resolve(inventory: MetadataInventory, c: Candidate):
    """(from table, from column, to table, to column), or a rejection reason."""
    ends = []
    for table_name, column_name in ((c.from_table, c.from_column), (c.to_table, c.to_column)):
        table = _find_table(inventory, table_name)
        if table is None:
            return None, "table not in inventory"
        column = _find_column(table, column_name)
        if column is None:
            return None, "column not in inventory"
        ends.append((table, column))
    (ft, fc), (tt, tc) = ends
    if ft is tt and fc.name == tc.name:
        return None, "self-relationship (same table and column)"
    return (ft, fc, tt, tc), None


def _confidence(
    th: Thresholds, compatible: bool, target_unique: bool | None, orphan: float | None, overlap: float | None, notes: list[str]
) -> str:
    if not compatible:
        return "low"
    if th.require_unique_target and target_unique is False:
        notes.append("The target column is not unique, so this can't be many-to-one")
        return "low"
    if orphan is None or overlap is None:
        return "low"
    if orphan <= th.high_orphan_max and overlap >= th.high_overlap_min:
        return "high"
    if orphan <= th.medium_orphan_max:
        return "medium"
    return "low"


def _unique(profile: TableProfile, column: str) -> bool | None:
    stats = profile.columns.get(column)
    if stats is None or stats.distinct_count is None or stats.null_count is None or profile.row_count is None:
        return None
    return stats.distinct_count == profile.row_count - stats.null_count


def _rejected(index: int, c: Candidate, reason: str) -> RelationshipEvidence:
    return RelationshipEvidence(
        id=f"R{index}",
        from_=ColumnRef(table=c.from_table, column=c.from_column),
        to=ColumnRef(table=c.to_table, column=c.to_column),
        source=c.source,
        status="rejected",
        rejected_reason=reason,
        evidence=RelationshipQueryEvidence(computed_at=utc_now()),
    )


def measure(
    connector: Any, inventory: MetadataInventory, candidates: list[Candidate], th: Thresholds
) -> list[RelationshipEvidence]:
    """Evidence for each candidate, in order, with ids R1, R2, ... Every table a candidate
    uses must already have a profile (the stage computes missing ones first)."""
    results: list[RelationshipEvidence] = []
    seen: set[tuple] = set()
    plans: list[tuple[int, Candidate, Any]] = []

    for c in candidates:
        resolved, reason = resolve(inventory, c)
        index = len(results) + 1
        if resolved is None:
            results.append(_rejected(index, c, reason))
            continue
        ft, fc, tt, tc = resolved
        identity = (table_key(ft.schema_name, ft.table_name), fc.name, table_key(tt.schema_name, tt.table_name), tc.name)
        if identity in seen:
            continue  # the same join proposed twice: keep the first
        seen.add(identity)
        results.append(None)  # placeholder keeps the order
        plans.append((index, c, resolved))

    with connector.connect() as connection:
        try:
            connection.timeout = QUERY_TIMEOUT_SECONDS
        except Exception:
            pass
        for index, c, (ft, fc, tt, tc) in plans:
            results[index - 1] = _measure_one(connection, index, c, ft, fc, tt, tc, th)
    return results


def _ref(table: InventoryTable, column: InventoryColumn) -> ColumnRef:
    return ColumnRef(table=table_key(table.schema_name, table.table_name), column=column.name)


def _measure_one(connection, index, c, ft, fc, tt, tc, th: Thresholds) -> RelationshipEvidence:
    fp, tp = ft.profile, tt.profile
    reject = lambda reason: _rejected(index, c, reason).model_copy(update={"from_": _ref(ft, fc), "to": _ref(tt, tc)})
    if fp is None or tp is None or fp.status == "error" or tp.status == "error":
        return reject("profile missing")
    if fp.columns.get(fc.name) is None or fp.columns[fc.name].null_count is None:
        return reject("profile missing: the source column type can't be analysed")
    if tp.columns.get(tc.name) is None or tp.columns[tc.name].null_count is None:
        return reject("profile missing: the target column type can't be analysed")

    compatible = types_compatible(fc.data_type, tc.data_type)
    null_rate = _percent(fp.columns[fc.name].null_count, fp.row_count) if fp.row_count else None
    target_unique = _unique(tp, tc.name)
    notes: list[str] = []
    base = dict(
        id=f"R{index}",
        from_=_ref(ft, fc),
        to=_ref(tt, tc),
        source=c.source,
        type_compatible=compatible,
        null_rate=null_rate,
        thresholds=th.as_dict(),
    )

    if not compatible:
        notes.append(f"Types differ ({fc.data_type} to {tc.data_type}); no join query was run")
        return RelationshipEvidence(
            **base,
            confidence="low",
            notes=notes,
            evidence=RelationshipQueryEvidence(computed_at=utc_now()),
        )

    source_rows = fp.row_count - fp.columns[fc.name].null_count
    if source_rows == 0:
        notes.append("The source column has no non-null values")
        return RelationshipEvidence(
            **base, confidence="low", notes=notes, evidence=RelationshipQueryEvidence(computed_at=utc_now())
        )

    s_col, t_col = quote_identifier(fc.name), quote_identifier(tc.name)
    s_tab, t_tab = qualified(ft.schema_name, ft.table_name), qualified(tt.schema_name, tt.table_name)
    target_keys = f"(SELECT DISTINCT {t_col} AS [k] FROM {t_tab} WHERE {t_col} IS NOT NULL) AS t"
    overlap_sql = (
        "SELECT COUNT_BIG(*), SUM(CASE WHEN t.[k] IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM (SELECT DISTINCT {s_col} AS [k] FROM {s_tab} WHERE {s_col} IS NOT NULL) AS s "
        f"LEFT JOIN {target_keys} ON s.[k] = t.[k]"
    )
    orphan_sql = (
        "SELECT COUNT_BIG(*), SUM(CASE WHEN t.[k] IS NULL THEN 1 ELSE 0 END) "
        f"FROM {s_tab} AS s LEFT JOIN {target_keys} ON s.{s_col} = t.[k] WHERE s.{s_col} IS NOT NULL"
    )
    queries: list[str] = []
    try:
        cursor = connection.cursor()
        queries.append(overlap_sql)
        cursor.execute(overlap_sql)
        distinct_rows, matched_distinct = cursor.fetchone()
        queries.append(orphan_sql)
        cursor.execute(orphan_sql)
        total_rows, orphan_rows = cursor.fetchone()
    except Exception as exc:  # one candidate failing must not stop the others
        code, _ = classify_error(exc)
        return reject(f"evidence query failed ({code})")

    overlap = _percent(int(matched_distinct or 0), int(distinct_rows)) if distinct_rows else None
    orphan = _percent(int(orphan_rows or 0), int(total_rows)) if total_rows else None
    source_unique = _unique(fp, fc.name)
    if target_unique is None:
        cardinality = None
    elif target_unique:
        cardinality = "one_to_one" if source_unique else "many_to_one"
    else:
        cardinality = "many_to_many"
        if source_unique:
            notes.append("The source is unique but the target is not: the join runs one-to-many from the source")

    return RelationshipEvidence(
        **base,
        overlap_pct=overlap,
        orphan_rate=orphan,
        cardinality=cardinality,
        confidence=_confidence(th, compatible, target_unique, orphan, overlap, notes),
        notes=notes,
        evidence=RelationshipQueryEvidence(queries=queries, computed_at=utc_now()),
    )
