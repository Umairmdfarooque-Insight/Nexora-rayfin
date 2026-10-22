// Display rules for the Inventory screen (T-304). They only read what the API returned:
// whether a run may be accepted is decided by the API (Gate G2), never here.

import type {
  ApprovalRecord,
  ColumnProfile,
  GateResult,
  InventoryColumn,
  InventoryTable,
  Me,
  RunDetail,
  TableProfile,
} from "./types.ts";

export const INVENTORY_ARTIFACT = "MetadataInventory";
export const DATA_ROLE = "data_engineer";
export const EXCEPTION_PREFIX = "Exception:";
export const PAGE_SIZE = 50;

export const INVENTORY_STALE_MESSAGE = "The inventory changed. Reload to see the latest version.";

/** The hash every approval of the inventory must send as `expected_hash`. */
export function inventoryHash(run: RunDetail | null): string | null {
  return run?.artifacts?.[INVENTORY_ARTIFACT]?.hash ?? null;
}

export function tableKey(table: Pick<InventoryTable, "schema" | "table_name">): string {
  return `${table.schema}.${table.table_name}`;
}

/** Tables whose "schema.table" name contains the query, ignoring case. */
export function filterTables(tables: InventoryTable[], query: string): InventoryTable[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return tables;
  return tables.filter((table) => tableKey(table).toLowerCase().includes(needle));
}

export function paginate<T>(
  items: T[],
  page: number,
  size: number = PAGE_SIZE,
): { items: T[]; page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(1, page), pages);
  return { items: items.slice((current - 1) * size, current * size), page: current, pages };
}

/** Every warning of a table: profile warnings, then profile and table errors. */
export function tableWarnings(table: InventoryTable): string[] {
  const out = [...(table.profile?.warnings ?? [])];
  if (table.profile?.status === "error") {
    out.push(`${table.profile.code ?? "ERROR"}: ${table.profile.message ?? "the table could not be profiled"}`);
  }
  for (const error of table.errors) out.push(`${error.code}: ${error.message}`);
  return out;
}

/** The column a warning is about: the longest column name the warning starts with. */
export function warningColumn(warning: string, columns: InventoryColumn[]): string | null {
  const names = columns
    .map((c) => c.name)
    .filter((name) => warning.startsWith(`${name} `))
    .sort((a, b) => b.length - a.length);
  return names[0] ?? null;
}

/** The evidence behind a warning: the queries that ran (those naming the column, if any do)
 * and the column's numbers. */
export function warningEvidence(
  profile: TableProfile,
  column: string | null,
): { queries: string[]; stats: ColumnProfile | null } {
  if (!column) return { queries: profile.evidence.queries, stats: null };
  const quoted = `[${column.replace(/]/g, "]]")}]`;
  const named = profile.evidence.queries.filter((q) => q.includes(quoted));
  return {
    queries: named.length > 0 ? named : profile.evidence.queries,
    stats: profile.columns[column] ?? null,
  };
}

/** The tables G2 treats as candidates. */
export function candidateTables(gate: GateResult | null): string[] {
  const list = gate?.checks.find((c) => c.id === "G2.1")?.evidence.candidates;
  return Array.isArray(list) ? (list as string[]) : [];
}

/** Candidates that still need an approved exception (check G2.5). */
export function missingExceptions(gate: GateResult | null): string[] {
  const list = gate?.checks.find((c) => c.id === "G2.5")?.evidence.missing_exceptions;
  return Array.isArray(list) ? (list as string[]) : [];
}

export function exceptionRecords(approvals: ApprovalRecord[]): ApprovalRecord[] {
  return approvals.filter((a) => a.artifact.startsWith(EXCEPTION_PREFIX));
}

export function hasDataRole(me: Me | null): boolean {
  return !!me && me.roles.includes(DATA_ROLE);
}

export function failedChecks(gate: GateResult | null): string[] {
  return gate ? gate.checks.filter((c) => c.status === "fail").map((c) => c.id) : [];
}

/** Accept needs a passing G2 on the hash the user is looking at, in REQUIREMENT_APPROVED. */
export function canAcceptInventory(
  run: RunDetail | null,
  gate: GateResult | null,
  me: Me | null,
): boolean {
  return (
    run?.state === "REQUIREMENT_APPROVED" &&
    hasDataRole(me) &&
    gate !== null &&
    gate.passed &&
    gate.artifact_hash === inventoryHash(run)
  );
}

/** Why Accept is unavailable, or null if it is available. */
export function acceptBlockedReason(
  run: RunDetail | null,
  gate: GateResult | null,
  me: Me | null,
): string | null {
  if (!run || !inventoryHash(run)) return "No metadata has been harvested yet.";
  if (run.state === "DATA_PROFILED") return "The inventory is accepted. Re-harvest to change it.";
  if (run.state !== "REQUIREMENT_APPROVED") return `A run in ${run.state} can't accept the inventory.`;
  if (!hasDataRole(me)) return `Only a ${DATA_ROLE} can accept the inventory or approve exceptions.`;
  if (!gate) return "Gate G2 hasn't been checked yet.";
  if (!gate.passed) return `Gate G2 failed: ${failedChecks(gate).join(", ")}.`;
  if (gate.artifact_hash !== inventoryHash(run)) {
    return "Gate G2 must be checked again for the latest inventory.";
  }
  return null;
}

/** Schemas typed as a comma-separated list. */
export function parseSchemas(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function formatPercent(value: number | null): string {
  return value === null ? "n/a" : `${value}%`;
}
