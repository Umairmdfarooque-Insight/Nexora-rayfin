"""The Gate G4 checks (T-504, FR-012, spec §12): references, relationships, DAX execution,
reconciliation and RLS of the deployed semantic model.

Every check is deterministic code. The only model call is the reference SQL of the reconciliation
(`recon_sql.py`), and its figures are compared by code. Each function returns `ValidationCheck`s;
the stage decides what a failure means for the run. Source data is only read, and the only writes
(deploy and refresh) go to the sandbox model, in the stage.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from app.connectors.powerbi_api import DaxError, PowerBiClient
from app.contracts.business_contract import BusinessContract, Kpi
from app.contracts.model_plan import ModelSpec
from app.contracts.validation_report import ValidationCheck, fingerprint
from app.discovery.data_profiler import classify_error, qualified, quote_identifier
from app.discovery.relationship_evidence import load_thresholds
from app.llm.errors import LlmTimeout, LlmUpstreamError, PromptRejected
from app.model.dax_generator import extract_references
from app.model.recon_sql import ReconSql, ReconSqlRejected
from app.model.tmdl_reader import TmdlModel, TmdlTable
from app.model.validation_config import Tolerance

MAX_SLICE_ROWS = 1000


def check(
    id_: str, obj: str, status: str, message: str, code: str | None = None, evidence: dict | None = None
) -> ValidationCheck:
    severity = {"fail": "error", "warning": "warning"}.get(status, "info")
    return ValidationCheck(
        id=id_, object=obj, status=status, severity=severity, code=code, message=message,
        evidence=evidence or {}, fingerprint=fingerprint(id_, obj, code, message),
    )


def skipped(id_: str, obj: str, reason: str) -> ValidationCheck:
    return check(id_, obj, "skipped", f"skipped: {reason}", code="SKIPPED")


@dataclass
class Context:
    spec: ModelSpec
    contract: BusinessContract
    tmdl: TmdlModel
    tolerance: Tolerance
    workspace_id: str
    dataset_id: str
    pbi: PowerBiClient
    sql_connector: Any
    recon_for: Callable[[Kpi], ReconSql]
    user_upn: str | None = None
    values: dict[str, float | None] = field(default_factory=dict)  # measure -> DAX total, set by check_dax
    baseline: dict[str, float | None] = field(default_factory=dict)  # measure -> total in the last validated earlier version


def _kpi_of(ctx: Context, measure: str) -> Kpi | None:
    spec_measure = next((m for m in ctx.spec.measures if m.name.casefold() == measure.casefold()), None)
    if spec_measure is None:
        return None
    return next((k for k in ctx.contract.kpis if k.name.casefold() == spec_measure.kpi.casefold()), None)


def numeric_examples(kpi: Kpi | None) -> list:
    return [e for e in (kpi.expected_examples if kpi else []) if isinstance(e.value, (int, float)) and not isinstance(e.value, bool)]


def to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _dax_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _dax_literal(label: Any) -> str | None:
    if isinstance(label, bool) or label is None:
        return None
    if isinstance(label, (int, float, Decimal)):
        return str(label)
    return '"' + str(label).replace('"', '""') + '"'


# --- G4.REF ---------------------------------------------------------------------------------


def unknown_references(dax: str, tables: dict[str, TmdlTable], measures: set[str]) -> list[str]:
    columns, bare, _ = extract_references(dax)
    by_name = {n.casefold(): t for n, t in tables.items()}
    problems = []
    for table, column in columns:
        t = by_name.get(table.casefold())
        if t is None:
            problems.append(f"table '{table}' not found: {table}[{column}]")
        elif column.casefold() not in {c.casefold() for c in t.columns}:
            problems.append(f"column '{column}' not found in table '{t.name}': {table}[{column}]")
    for name in bare:
        if name.casefold() not in measures:
            problems.append(f"measure or column [{name}] not found")
    return problems


def check_references(ctx: Context) -> list[ValidationCheck]:
    tables = ctx.tmdl.tables
    measures = {m.casefold() for t in tables.values() for m in t.measures}
    out: list[ValidationCheck] = []
    count = 0
    for t in tables.values():
        for name, dax in t.measures.items():
            count += 1
            for problem in unknown_references(dax, tables, measures):
                out.append(check("G4.REF", f"measure:{name}", "fail", f"Measure [{name}]: {problem}", "UNKNOWN_REFERENCE", {"dax": dax}))
        for h, levels in t.hierarchies.items():
            count += 1
            for level, column in levels:
                if column.casefold() not in {c.casefold() for c in t.columns}:
                    out.append(check("G4.REF", f"hierarchy:{t.name}.{h}", "fail", f"Hierarchy '{h}' level '{level}' uses column '{column}', which is not in table '{t.name}'", "UNKNOWN_REFERENCE"))
    by_name = {n.casefold(): t for n, t in tables.items()}
    for r in ctx.tmdl.relationships:
        count += 1
        label = f"{r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}"
        for table, column in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
            t = by_name.get(table.casefold())
            if t is None or column.casefold() not in {c.casefold() for c in t.columns}:
                out.append(check("G4.REF", f"relationship:{label}", "fail", f"Relationship {label}: {table}.{column} is not in the model", "UNKNOWN_REFERENCE"))
    if not out:
        out.append(check("G4.REF", "model", "pass", f"{count} measures, relationships and hierarchies reference existing objects"))
    return out


# --- G4.REL ---------------------------------------------------------------------------------


def _source_of(ctx: Context, table: str, column: str) -> tuple[str, str, str] | None:
    t = next((t for n, t in ctx.tmdl.tables.items() if n.casefold() == table.casefold()), None)
    if t is None or t.source is None:
        return None
    col = next((c for n, c in t.columns.items() if n.casefold() == column.casefold()), None)
    return t.source[0], t.source[1], (col.source_column or col.name) if col else column


def check_relationships(ctx: Context, connection: Any) -> list[ValidationCheck]:
    if not ctx.tmdl.relationships:
        return [check("G4.REL", "model", "pass", "no relationships to check")]
    threshold = load_thresholds().medium_orphan_max
    out = []
    for r in ctx.tmdl.relationships:
        label = f"{r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}"
        obj = f"relationship:{label}"
        src, dst = _source_of(ctx, r.from_table, r.from_column), _source_of(ctx, r.to_table, r.to_column)
        if src is None or dst is None:
            out.append(check("G4.REL", obj, "fail", f"Relationship {label}: a table has no Lakehouse source to check", "NO_SOURCE"))
            continue
        (fs, ft, fc), (ts, tt, tc) = src, dst
        f_tab, t_tab, f_col, t_col = qualified(fs, ft), qualified(ts, tt), quote_identifier(fc), quote_identifier(tc)
        unique_sql = lambda tab, col: f"SELECT COUNT_BIG(*), COUNT_BIG(DISTINCT {col}) FROM {tab} WHERE {col} IS NOT NULL"
        orphan_sql = (
            "SELECT COUNT_BIG(*), SUM(CASE WHEN t.[k] IS NULL THEN 1 ELSE 0 END) "
            f"FROM {f_tab} AS s LEFT JOIN (SELECT DISTINCT {t_col} AS [k] FROM {t_tab} WHERE {t_col} IS NOT NULL) AS t "
            f"ON s.{f_col} = t.[k] WHERE s.{f_col} IS NOT NULL"
        )
        queries: list[str] = []
        problems: list[str] = []
        evidence: dict[str, Any] = {"from": f"{fs}.{ft}.{fc}", "to": f"{ts}.{tt}.{tc}", "queries": queries}
        warn = False
        try:
            cursor = connection.cursor()
            sides = [("to", t_tab, t_col)] if r.to_cardinality == "one" else []
            if r.from_cardinality == "one":
                sides.append(("from", f_tab, f_col))
            for side, tab, col in sides:
                sql = unique_sql(tab, col)
                queries.append(sql)
                cursor.execute(sql)
                total, distinct = cursor.fetchone()
                duplicates = int(total or 0) - int(distinct or 0)
                evidence[f"{side}_rows"], evidence[f"{side}_duplicate_keys"] = int(total or 0), duplicates
                if duplicates > 0:
                    problems.append(f"{duplicates} duplicate key value(s) on the 'one' side ({side}: {tab}.{col})")
            queries.append(orphan_sql)
            cursor.execute(orphan_sql)
            rows, orphans = cursor.fetchone()
            rows, orphans = int(rows or 0), int(orphans or 0)
            rate = round(100.0 * orphans / rows, 4) if rows else 0.0
            evidence.update({"source_rows": rows, "orphan_rows": orphans, "orphan_rate_pct": rate, "orphan_rate_max_pct": threshold})
            if rate > threshold:
                problems.append(f"orphan rate {rate}% is above {threshold}%")
            elif orphans:
                warn = True
        except Exception as exc:  # one relationship failing must not stop the others
            code, message = classify_error(exc)
            out.append(check("G4.REL", obj, "fail", f"Relationship {label}: the check query failed ({message})", code, evidence))
            continue
        if problems:
            out.append(check("G4.REL", obj, "fail", f"Relationship {label}: " + "; ".join(problems), "RELATIONSHIP_INVALID", evidence))
        elif warn:
            out.append(check("G4.REL", obj, "warning", f"Relationship {label}: {evidence['orphan_rows']} source row(s) have no match ({evidence['orphan_rate_pct']}%)", "ORPHANS", evidence))
        else:
            out.append(check("G4.REL", obj, "pass", f"Relationship {label}: keys are unique and every source row matches", None, evidence))
    return out


# --- G4.DAX ---------------------------------------------------------------------------------


def _first_value(rows: list[dict]) -> Any:
    return next(iter(rows[0].values()), None) if rows else None


def check_dax(ctx: Context) -> list[ValidationCheck]:
    out = []
    measures = [(n, e) for t in ctx.tmdl.tables.values() for n, e in sorted(t.measures.items())]
    if not measures:
        return [check("G4.DAX", "model", "pass", "no measures with DAX to execute")]
    for name, dax in sorted(measures):
        query = f'EVALUATE ROW("v", {_dax_name(name)})'
        try:
            value = to_number(_first_value(ctx.pbi.execute_dax(ctx.workspace_id, ctx.dataset_id, query)))
        except DaxError as exc:
            ctx.values.pop(name, None)
            out.append(check("G4.DAX", f"measure:{name}", "fail", f"Measure [{name}] failed to execute: {exc}", "DAX_ERROR", {"query": query, "dax": dax, "response": str(exc)}))
            continue
        ctx.values[name] = value
        evidence = {"query": query, "value": value}
        if value is None:
            expected = numeric_examples(_kpi_of(ctx, name))
            if expected:
                out.append(check("G4.DAX", f"measure:{name}", "fail", f"Measure [{name}] returned blank but a value is expected", "BLANK_RESULT", evidence | {"expected": [e.value for e in expected]}))
            else:
                out.append(check("G4.DAX", f"measure:{name}", "warning", f"Measure [{name}] returned blank and no expected value exists", "BLANK_RESULT", evidence))
        else:
            out.append(check("G4.DAX", f"measure:{name}", "pass", f"Measure [{name}] executed: {value}", None, evidence))
    return out


# --- G4.RECON -------------------------------------------------------------------------------


def _compare(tol: Tolerance, dax: float | None, reference: float | None) -> tuple[bool, float | None]:
    a, b = dax or 0.0, reference or 0.0
    return tol.allows(a, b), round(a - b, 10)


def _run_sql(connection: Any, sql: str) -> list[tuple]:
    cursor = connection.cursor()
    cursor.execute(sql)
    return [tuple(r) for r in cursor.fetchall()] if sql else []


def check_reconciliation(ctx: Context, connection: Any) -> list[ValidationCheck]:
    out = []
    for name in sorted(ctx.values):
        obj = f"measure:{name}"
        kpi = _kpi_of(ctx, name)
        if kpi is None:
            out.append(check("G4.RECON", obj, "fail", f"Measure [{name}]: its KPI is not in the approved contract", "KPI_MISSING"))
            continue
        dax_value = ctx.values[name]
        evidence: dict[str, Any] = {"dax_query": f'EVALUATE ROW("v", {_dax_name(name)})', "dax_value": dax_value}
        failures: list[str] = []
        warnings: list[str] = []
        notes: list[str] = []

        examples = numeric_examples(kpi)
        matched_example = False
        if len(examples) == 1:
            ok, diff = _compare(ctx.tolerance, dax_value, float(examples[0].value))
            evidence["expected_example"] = {"description": examples[0].description, "value": examples[0].value, "difference": diff}
            matched_example = ok
            if not ok:
                failures.append(f"DAX value {dax_value} differs from the business example {examples[0].value} by {diff}")
        elif examples:
            evidence["expected_examples"] = [{"description": e.description, "value": e.value} for e in examples]
            warnings.append("several business examples are given and can't be matched to the total automatically: review them")
        else:
            notes.append("no business example provided")

        try:
            recon = ctx.recon_for(kpi)
        except (ReconSqlRejected, LlmTimeout, LlmUpstreamError, PromptRejected) as exc:
            reason = exc.reason if isinstance(exc, ReconSqlRejected) else str(exc)
            evidence["sql_reference_error"] = reason
            if matched_example and not failures:
                warnings.append(f"the independent SQL reference is unavailable ({reason}); only the business example was compared")
            else:
                failures.append(f"no independent SQL reference could be produced: {reason}")
            recon = None

        if recon is not None:
            evidence["sql_query"] = recon.total_sql
            try:
                rows = _run_sql(connection, recon.total_sql)
                sql_value = to_number(rows[0][0]) if rows and rows[0] else None
            except Exception as exc:
                code, message = classify_error(exc)
                failures.append(f"the reference SQL failed ({code}: {message})")
                sql_value, evidence["sql_error"] = None, message
            else:
                evidence["sql_value"] = sql_value
                ok, diff = _compare(ctx.tolerance, dax_value, sql_value)
                evidence["difference"] = diff
                if not ok:
                    failures.append(f"DAX value {dax_value} differs from the SQL value {sql_value} by {diff}")
            if recon.slice_sql and recon.slice_table and recon.slice_column and not any("reference SQL failed" in f for f in failures):
                _slice(ctx, connection, name, recon, evidence, failures, warnings)

        evidence["tolerance"] = {"relative": ctx.tolerance.relative, "absolute": ctx.tolerance.absolute}
        evidence["notes"] = notes
        if failures:
            out.append(check("G4.RECON", obj, "fail", f"Measure [{name}]: " + "; ".join(failures), "RECON_MISMATCH" if any("differs" in f for f in failures) else "RECON_REFERENCE_UNAVAILABLE", evidence))
        elif warnings:
            out.append(check("G4.RECON", obj, "warning", f"Measure [{name}]: " + "; ".join(warnings), "RECON_PARTIAL", evidence))
        else:
            out.append(check("G4.RECON", obj, "pass", f"Measure [{name}] matches the independent figure ({dax_value})", None, evidence))
    if not out:
        out.append(check("G4.RECON", "model", "pass", "no measures to reconcile"))
    return out


def _slice(ctx, connection, name, recon: ReconSql, evidence: dict, failures: list, warnings: list) -> None:
    sliced: dict[str, Any] = {"table": recon.slice_table, "column": recon.slice_column, "sql_query": recon.slice_sql}
    evidence["slice"] = sliced
    try:
        cursor = connection.cursor()
        cursor.execute(recon.slice_sql)
        rows = [tuple(r) for r in cursor.fetchmany(MAX_SLICE_ROWS)]
    except Exception as exc:
        code, message = classify_error(exc)
        failures.append(f"the slice SQL failed ({code}: {message})")
        return
    labelled = sorted((r for r in rows if len(r) >= 2 and r[0] is not None), key=lambda r: str(r[0]))
    if not labelled:
        warnings.append("the slice query returned no rows to compare")
        return
    label, sql_value = labelled[0][0], to_number(labelled[0][1])
    literal = _dax_literal(label)
    if literal is None:
        warnings.append("the slice label type can't be compared")
        return
    table = f"'{recon.slice_table}'" if not recon.slice_table.isidentifier() else recon.slice_table
    query = f'EVALUATE ROW("v", CALCULATE({_dax_name(name)}, {table}{_dax_name(recon.slice_column)} = {literal}))'
    sliced.update({"label": str(label), "dax_query": query, "sql_value": sql_value})
    try:
        dax_value = to_number(_first_value(ctx.pbi.execute_dax(ctx.workspace_id, ctx.dataset_id, query)))
    except DaxError as exc:
        failures.append(f"the sliced DAX query failed: {exc}")
        return
    ok, diff = _compare(ctx.tolerance, dax_value, sql_value)
    sliced.update({"dax_value": dax_value, "difference": diff})
    if not ok:
        failures.append(f"sliced by {recon.slice_table}[{recon.slice_column}] = {label}: DAX value {dax_value} differs from the SQL value {sql_value} by {diff}")


# --- G4.RLS ---------------------------------------------------------------------------------


def check_rls(ctx: Context) -> list[ValidationCheck]:
    roles = ctx.spec.roles
    if not roles:
        return [check("G4.RLS", "model", "pass", "no roles defined")]
    out = []
    measures = {m.casefold() for t in ctx.tmdl.tables.values() for m in t.measures}
    for role in roles:
        obj = f"role:{role.name}"
        defined = ctx.tmdl.roles.get(role.name)
        if defined is None:
            out.append(check("G4.RLS", obj, "fail", f"Role {role.name} is not defined in the model: its filter expression is missing", "RLS_ROLE_MISSING", {"description": role.description}))
            continue
        if not defined.filters:
            out.append(check("G4.RLS", obj, "fail", f"Role {role.name} has no filter expression", "RLS_FILTER_MISSING"))
            continue
        problems = []
        for table, expression in defined.filters.items():
            if table.casefold() not in {n.casefold() for n in ctx.tmdl.tables}:
                problems.append(f"table '{table}' not found")
            problems += unknown_references(expression, ctx.tmdl.tables, measures)
        if problems:
            out.append(check("G4.RLS", obj, "fail", f"Role {role.name}: invalid filter expression: " + "; ".join(problems), "RLS_FILTER_INVALID", {"filters": defined.filters}))
            continue
        evidence: dict[str, Any] = {"filters": defined.filters, "identity": ctx.user_upn}
        status, message, code = "pass", f"Role {role.name}: the filter expression is valid", None
        for table in sorted(defined.filters):
            query = f'EVALUATE ROW("n", COUNTROWS(\'{table}\'))'
            try:
                full = to_number(_first_value(ctx.pbi.execute_dax(ctx.workspace_id, ctx.dataset_id, query)))
                filtered = to_number(_first_value(ctx.pbi.execute_dax(ctx.workspace_id, ctx.dataset_id, query, impersonate=ctx.user_upn)))
            except DaxError as exc:
                status, message, code = "fail", f"Role {role.name}: the query as {ctx.user_upn} failed: {exc}", "RLS_QUERY_FAILED"
                break
            evidence[table] = {"rows_without_identity": full, "rows_with_identity": filtered}
            if status == "pass" and full is not None and filtered is not None and filtered >= full:
                status, message, code = "warning", f"Role {role.name}: the query as {ctx.user_upn} returned all {int(full)} rows of {table}; confirm that identity is a member of the role", "RLS_NOT_FILTERING"
        out.append(check("G4.RLS", obj, status, message, code, evidence))
    return out
