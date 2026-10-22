"""Data profiling over the Fabric SQL endpoint.

`profile_tables` (T-302) is the generic profiler: read-only SELECTs for the tables of a
harvested inventory. Identifiers come from the inventory and are always bracket-quoted.
No row values are stored: only counts, and min and max of date and numeric columns.

`profile_product_ids` is the old sample-based prototype, kept for `app/test_data_profiler.py`.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.contracts.metadata_inventory import (
    ColumnProfile,
    Freshness,
    InventoryColumn,
    InventoryTable,
    MetadataInventory,
    ProfileEvidence,
    TableProfile,
)
from app.discovery.glossary import load_glossary, normalize
from app.workflow.run import utc_now

QUERY_TIMEOUT_SECONDS = 120
APPROX_ROW_THRESHOLD = 10_000_000
COLUMNS_PER_QUERY = 20

_NUMERIC = {
    "BYTE", "TINYINT", "SHORT", "SMALLINT", "INT", "INTEGER", "LONG", "BIGINT",
    "FLOAT", "REAL", "DOUBLE", "DECIMAL", "DEC", "NUMERIC",
}
_DATE = {"DATE", "TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "DATETIME", "DATETIME2", "SMALLDATETIME"}
_OTHER_COUNTABLE = {"STRING", "VARCHAR", "CHAR", "NVARCHAR", "NCHAR", "TEXT", "BOOLEAN", "BIT", "UUID", "GUID"}
_FRESHNESS_HINTS = ("modif", "updat", "refresh", "load", "ingest", "creat")


def quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qualified(schema: str, table: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def column_kind(data_type: str | None) -> str:
    """'numeric', 'date', 'other' (distinct count only) or 'skip' (can't be counted)."""
    base = (data_type or "").upper()
    for separator in ("(", "<", " "):
        base = base.split(separator)[0]
    if base in _NUMERIC:
        return "numeric"
    if base in _DATE:
        return "date"
    if base in _OTHER_COUNTABLE:
        return "other"
    return "skip"


def is_key_candidate(name: str, extra_keys: set[str] | None = None) -> bool:
    lowered = name.lower()
    return lowered.endswith("id") or lowered.endswith("key") or name in (extra_keys or set())


def table_key(table: InventoryTable) -> str:
    return f"{table.schema_name}.{table.table_name}"


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%dT00:00:00Z")
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _age_days(value: Any) -> int | None:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        return None
    return max(0, (datetime.now(timezone.utc) - moment).days)


def classify_error(exc: Exception) -> tuple[str, str]:
    """A table-level error code and a message that is safe to store."""
    state = str(exc.args[0]) if exc.args else ""
    text = " ".join(str(a) for a in exc.args).lower()
    if state == "42S02" or "invalid object name" in text:
        return "TABLE_NOT_FOUND", "The table no longer exists in the SQL endpoint"
    if state in ("HYT00", "HYT01") or "timeout" in text or "timed out" in text:
        return "QUERY_TIMEOUT", f"The query did not finish within {QUERY_TIMEOUT_SECONDS} seconds"
    return "QUERY_FAILED", f"The profiling query failed (SQLSTATE {state if len(state) == 5 else 'unknown'})"


def matching_keys(contract: Any, keys: list[str]) -> list[str]:
    """The "schema.table" names in `keys` whose table part matches a contract entity, using
    the glossary (config/glossary.yaml) and name normalization. Deterministic; no LLM."""
    if contract is None:
        return []
    glossary = load_glossary()
    found: list[str] = []
    for entity in contract.entities:
        aliases = glossary.aliases_for(entity)
        for key in keys:
            name = normalize(key.partition(".")[2])
            if key not in found and any(alias in name for alias in aliases):
                found.append(key)
    return found


def candidate_tables(contract: Any, inventory: MetadataInventory) -> list[str]:
    """Harvested tables ("schema.table") that match a contract entity."""
    return matching_keys(contract, [table_key(t) for t in inventory.tables])


def candidate_error_tables(contract: Any, inventory: MetadataInventory) -> list[str]:
    """Tables that match a contract entity but failed to harvest (so they aren't in
    `inventory.tables`). Gate G2 must not pass over them silently."""
    harvested = {table_key(t).lower() for t in inventory.tables}
    failed = sorted({e.table for e in inventory.errors if e.table.lower() not in harvested})
    return matching_keys(contract, failed)


def _freshness_column(table: InventoryTable) -> InventoryColumn | None:
    dates = [c for c in table.columns if column_kind(c.data_type) == "date"]
    for hint in _FRESHNESS_HINTS:
        for column in dates:
            if hint in column.name.lower():
                return column
    return dates[0] if dates else None


def _profile_table(cursor: Any, table: InventoryTable, extra_keys: set[str] | None) -> TableProfile:
    target = qualified(table.schema_name, table.table_name)
    queries: list[str] = []

    def run(sql: str) -> Any:
        queries.append(sql)
        cursor.execute(sql)
        return cursor.fetchone()

    row_count = int(run(f"SELECT COUNT_BIG(*) FROM {target}")[0])
    approximate = row_count > APPROX_ROW_THRESHOLD

    warnings: list[str] = []
    columns: dict[str, ColumnProfile] = {}
    raw_max: dict[str, Any] = {}
    countable: list[tuple[InventoryColumn, str]] = []
    for column in table.columns:
        kind = column_kind(column.data_type)
        if kind == "skip":
            columns[column.name] = ColumnProfile(
                is_key_candidate=is_key_candidate(column.name, extra_keys),
                note=f"{column.data_type} columns are not profiled",
            )
        else:
            countable.append((column, kind))

    if row_count == 0:
        warnings.append("The table is empty")
        for column, _ in countable:
            columns[column.name] = ColumnProfile(
                null_count=0, distinct_count=0, is_key_candidate=is_key_candidate(column.name, extra_keys)
            )
    else:
        for start in range(0, len(countable), COLUMNS_PER_QUERY):
            chunk = countable[start : start + COLUMNS_PER_QUERY]
            parts: list[str] = []
            for column, kind in chunk:
                c = quote_identifier(column.name)
                parts.append(f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END)")
                parts.append(f"APPROX_COUNT_DISTINCT({c})" if approximate else f"COUNT(DISTINCT {c})")
                if kind in ("numeric", "date"):
                    parts.append(f"MIN({c})")
                    parts.append(f"MAX({c})")
            row = run(f"SELECT {', '.join(parts)} FROM {target}")

            position = 0
            for column, kind in chunk:
                nulls = int(row[position] or 0)
                distinct = int(row[position + 1] or 0)
                position += 2
                low = high = None
                if kind in ("numeric", "date"):
                    low, high = row[position], row[position + 1]
                    position += 2
                key = is_key_candidate(column.name, extra_keys)
                profile = ColumnProfile(
                    null_count=nulls,
                    distinct_count=distinct,
                    is_key_candidate=key,
                    # Exact only: an approximate distinct count can't prove uniqueness.
                    is_unique=(distinct == row_count - nulls) if key and not approximate else None,
                    min=_json_value(low),
                    max=_json_value(high),
                    approximate=approximate,
                )
                columns[column.name] = profile
                raw_max[column.name] = high

                if nulls == row_count:
                    warnings.append(f"{column.name} is entirely null")
                elif key and nulls:
                    warnings.append(f"{column.name} has {nulls} null value(s)")
                if profile.is_unique is False:
                    c = quote_identifier(column.name)
                    dup = run(
                        f"SELECT COUNT_BIG(*) FROM (SELECT {c} FROM {target} "
                        f"WHERE {c} IS NOT NULL GROUP BY {c} HAVING COUNT(*) > 1) AS d"
                    )
                    profile.duplicate_count = int(dup[0])
                    warnings.append(f"{column.name} is not unique ({profile.duplicate_count} duplicate values)")

    freshness = Freshness(status="unknown")
    fcol = _freshness_column(table)
    if fcol is not None and columns.get(fcol.name) and columns[fcol.name].max is not None:
        freshness = Freshness(
            column=fcol.name,
            max_value=columns[fcol.name].max,
            age_days=_age_days(raw_max.get(fcol.name)),
            status="ok",
        )

    return TableProfile(
        status="warning" if warnings else "ok",
        row_count=row_count,
        columns=columns,
        freshness=freshness,
        warnings=warnings,
        evidence=ProfileEvidence(queries=queries, profiled_at=utc_now()),
    )


def profile_tables(
    connector: Any,
    inventory: MetadataInventory,
    tables: list[str],
    extra_key_columns: dict[str, set[str]] | None = None,
) -> dict[str, TableProfile]:
    """Profile the named tables ("schema.table", all present in `inventory`).

    A table that fails (removed since the harvest, timeout, any query error) gets a profile
    with status "error" and a code; the others continue. `extra_key_columns` maps a table to
    columns that count as key candidates because a relationship names them.
    """
    by_key = {table_key(t).lower(): t for t in inventory.tables}
    profiles: dict[str, TableProfile] = {}
    with connector.connect() as connection:
        try:
            connection.timeout = QUERY_TIMEOUT_SECONDS  # per-statement timeout, in seconds
        except Exception:
            pass
        for name in dict.fromkeys(tables):
            table = by_key[name.lower()]
            key = table_key(table)
            cursor = connection.cursor()
            try:
                profiles[key] = _profile_table(cursor, table, (extra_key_columns or {}).get(key))
            except Exception as exc:  # one failing table must not stop the others
                code, message = classify_error(exc)
                profiles[key] = TableProfile(
                    status="error",
                    code=code,
                    message=message,
                    evidence=ProfileEvidence(profiled_at=utc_now()),
                )
    return profiles


def profile_product_ids(connector: Any) -> dict[str, Any]:
    """Inspect product IDs in both tables using read-only SQL."""

    with connector.connect() as connection:
        cursor = connection.cursor()

        # Inspect product IDs in the sales table.
        cursor.execute("""
            SELECT TOP 10 product_id
            FROM saleslt.synthetic_sales_daily
            WHERE product_id IS NOT NULL
        """)
        sales_product_ids = [row[0] for row in cursor.fetchall()]

        # Inspect product IDs in the product dimension.
        cursor.execute("""
            SELECT TOP 10 ProductID
            FROM saleslt.SalesLT_Product
            WHERE ProductID IS NOT NULL
        """)
        dimension_product_ids = [row[0] for row in cursor.fetchall()]

    return {
        "sales_product_ids": sales_product_ids,
        "dimension_product_ids": dimension_product_ids,
    }
