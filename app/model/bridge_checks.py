"""The extra Gate G4 checks for bridge tables (T-506): G4.BRIDGE.UNIQUE, G4.BRIDGE.ORPHANS and
G4.BRIDGE.TOTALS. They run for every table of role `bridge` in the model, over the SQL endpoint
(read-only aggregates) and the measure totals G4.DAX already computed."""

from typing import Any

from app.contracts.validation_report import ValidationCheck
from app.discovery.data_profiler import classify_error, qualified, quote_identifier
from app.discovery.relationship_evidence import load_thresholds
from app.model.validators import Context, _compare, _source_of, check

DUPLICATE_SAMPLE = 5


def _unique_and_empty(ctx: Context, connection: Any, t, rels, sources) -> ValidationCheck:
    obj = f"bridge:{t.name}"
    schema, table = sources[0][0], sources[0][1]
    a, b = quote_identifier(sources[0][2]), quote_identifier(sources[1][2])
    bridge_tab = qualified(schema, table)
    evidence: dict[str, Any] = {"table": f"{schema}.{table}", "pair": [sources[0][2], sources[1][2]], "queries": []}
    queries = evidence["queries"]
    try:
        cursor = connection.cursor()
        count_sql = f"SELECT COUNT_BIG(*) FROM {bridge_tab}"
        queries.append(count_sql)
        cursor.execute(count_sql)
        rows = int(cursor.fetchone()[0] or 0)
        evidence["rows"] = rows
        dup_sql = (
            f"SELECT COUNT_BIG(*) FROM (SELECT {a}, {b} FROM {bridge_tab} WHERE {a} IS NOT NULL AND {b} IS NOT NULL "
            f"GROUP BY {a}, {b} HAVING COUNT_BIG(*) > 1) AS d"
        )
        queries.append(dup_sql)
        cursor.execute(dup_sql)
        duplicates = int(cursor.fetchone()[0] or 0)
        evidence["duplicate_pairs"] = duplicates
        if duplicates:
            sample_sql = (
                f"SELECT TOP {DUPLICATE_SAMPLE} {a}, {b}, COUNT_BIG(*) FROM {bridge_tab} WHERE {a} IS NOT NULL AND {b} IS NOT NULL "
                f"GROUP BY {a}, {b} HAVING COUNT_BIG(*) > 1 ORDER BY {a}, {b}"
            )
            queries.append(sample_sql)
            cursor.execute(sample_sql)
            evidence["duplicates"] = [{"pair": [r[0], r[1]], "count": int(r[2])} for r in cursor.fetchall()]
    except Exception as exc:
        code, message = classify_error(exc)
        return check("G4.BRIDGE.UNIQUE", obj, "fail", f"Bridge {t.name}: the check query failed ({message})", code, evidence)
    if rows == 0:
        return check("G4.BRIDGE.UNIQUE", obj, "fail", f"Bridge {t.name}: empty bridge", "BRIDGE_EMPTY", evidence)
    if duplicates:
        pairs = ", ".join(f"({d['pair'][0]}, {d['pair'][1]})" for d in evidence.get("duplicates", []))
        return check("G4.BRIDGE.UNIQUE", obj, "fail", f"Bridge {t.name}: {duplicates} duplicate pair(s), for example {pairs}", "BRIDGE_DUPLICATE_PAIRS", evidence)
    return check("G4.BRIDGE.UNIQUE", obj, "pass", f"Bridge {t.name}: {rows} rows, no duplicate pairs", None, evidence)


def _orphans(ctx: Context, connection: Any, t, rel, threshold: float) -> ValidationCheck:
    src = _source_of(ctx, rel.from_table, rel.from_column)
    dst = _source_of(ctx, rel.to_table, rel.to_column)
    obj = f"relationship:{t.name}.{rel.from_column} -> {rel.to_table}.{rel.to_column}"
    if src is None or dst is None:
        return check("G4.BRIDGE.ORPHANS", obj, "fail", f"Bridge {t.name}: a table has no Lakehouse source to check", "NO_SOURCE")
    f_col, t_col = quote_identifier(src[2]), quote_identifier(dst[2])
    sql = (
        "SELECT COUNT_BIG(*), SUM(CASE WHEN d.[k] IS NULL THEN 1 ELSE 0 END) "
        f"FROM {qualified(src[0], src[1])} AS s LEFT JOIN (SELECT DISTINCT {t_col} AS [k] FROM {qualified(dst[0], dst[1])} "
        f"WHERE {t_col} IS NOT NULL) AS d ON s.{f_col} = d.[k] WHERE s.{f_col} IS NOT NULL"
    )
    ev: dict[str, Any] = {"queries": [sql], "bridge": f"{src[0]}.{src[1]}.{src[2]}", "dimension": f"{dst[0]}.{dst[1]}.{dst[2]}"}
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        total, orphans = cursor.fetchone()
    except Exception as exc:
        code, message = classify_error(exc)
        return check("G4.BRIDGE.ORPHANS", obj, "fail", f"Bridge {t.name}: the orphan query failed ({message})", code, ev)
    total, orphans = int(total or 0), int(orphans or 0)
    rate = round(100.0 * orphans / total, 4) if total else 0.0
    ev.update({"rows": total, "orphan_rows": orphans, "orphan_rate_pct": rate, "orphan_rate_max_pct": threshold})
    if rate > threshold:
        return check("G4.BRIDGE.ORPHANS", obj, "fail", f"Bridge {t.name}: {orphans} of {total} rows ({rate}%) reference a key that {rel.to_table} doesn't have", "BRIDGE_ORPHANS", ev)
    if orphans:
        return check("G4.BRIDGE.ORPHANS", obj, "warning", f"Bridge {t.name}: {orphans} of {total} rows ({rate}%) reference an unknown {rel.to_table} key", "BRIDGE_ORPHANS", ev)
    return check("G4.BRIDGE.ORPHANS", obj, "pass", f"Bridge {t.name}: every row matches a {rel.to_table} key", None, ev)


def _totals(ctx: Context) -> list[ValidationCheck]:
    if not ctx.values:
        return [check("G4.BRIDGE.TOTALS", "model", "pass", "no measures with DAX to compare")]
    if not ctx.baseline:
        return [check("G4.BRIDGE.TOTALS", "model", "warning", "no earlier validated version to compare the grand totals with: validate the model before adding the bridge", "NO_BASELINE")]
    out = []
    for name in sorted(ctx.values):
        if name not in ctx.baseline:
            out.append(check("G4.BRIDGE.TOTALS", f"measure:{name}", "warning", f"Measure [{name}] has no total from the earlier version", "NO_BASELINE"))
            continue
        before = ctx.baseline[name]
        ok, diff = _compare(ctx.tolerance, ctx.values[name], before)
        evidence = {"total_before": before, "total_with_bridge": ctx.values[name], "difference": diff}
        if ok:
            out.append(check("G4.BRIDGE.TOTALS", f"measure:{name}", "pass", f"Measure [{name}]: the grand total is unchanged by the bridge ({before})", None, evidence))
        else:
            out.append(check("G4.BRIDGE.TOTALS", f"measure:{name}", "fail", f"Measure [{name}]: the grand total changed from {before} to {ctx.values[name]} by {diff} once the bridge was added (double counting?)", "BRIDGE_TOTALS_CHANGED", evidence))
    return out


def check_bridges(ctx: Context, connection: Any) -> list[ValidationCheck]:
    """The bridge checks of the model; none if it has no bridge table."""
    bridges = [t for t in ctx.spec.tables if t.role == "bridge"]
    if not bridges:
        return []
    threshold = load_thresholds().medium_orphan_max
    out: list[ValidationCheck] = []
    for t in bridges:
        rels = [r for r in ctx.tmdl.relationships if r.from_table.casefold() == t.name.casefold()]
        keys = list(dict.fromkeys(r.from_column for r in rels))
        sources = [_source_of(ctx, t.name, k) for k in keys]
        if len(rels) < 2 or len(keys) < 2 or any(s is None for s in sources):
            out.append(check("G4.BRIDGE.UNIQUE", f"bridge:{t.name}", "fail", f"Bridge {t.name}: it needs a relationship to each of two tables, on two different key columns", "BRIDGE_RELATIONSHIPS"))
            continue
        out.append(_unique_and_empty(ctx, connection, t, rels, sources))
        out += [_orphans(ctx, connection, t, rel, threshold) for rel in rels[:2]]
    return out + _totals(ctx)
