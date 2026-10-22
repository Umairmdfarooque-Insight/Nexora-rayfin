// Display rules for the audit timeline and the trace of a visual (T-703). They only read what the
// API returned: whether the event chain is intact is decided by the API, never here.

import type { AuditEvent, AuditPage, ProfileEvidence, TraceResult } from "./types.ts";

export const AUDIT_PAGE_SIZE = 200;

const TYPE_LABELS: Record<string, string> = {
  state: "State",
  approval: "Approval",
  artifact: "Artifact",
  gate: "Gate",
  validation: "Validation",
  deployment: "Deployment",
  return: "Return",
};

export function eventTypeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type;
}

/** The Fluent badge colour of an event type. */
export function eventTypeColor(type: string): "brand" | "success" | "warning" | "danger" | "informative" | "important" {
  switch (type) {
    case "approval":
      return "success";
    case "gate":
    case "validation":
      return "informative";
    case "deployment":
      return "important";
    case "return":
      return "warning";
    default:
      return "brand";
  }
}

/** `sha256:3f2b1c4d…` for a long hash, so a row stays readable (the full hash is the tooltip). */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return "";
  const hex = hash.startsWith("sha256:") ? hash.slice(7) : hash;
  return `sha256:${hex.slice(0, 10)}…`;
}

/** The warning shown above the timeline when the chain is broken; null when it is intact. */
export function chainWarning(page: Pick<AuditPage, "chain_valid" | "first_broken_seq">): string | null {
  if (page.chain_valid) return null;
  const where = page.first_broken_seq ? `at event ${page.first_broken_seq}` : "";
  return `The audit log was changed: its hash chain is broken ${where}`.trim() + ". Events from there on can't be trusted.";
}

export function pageRange(offset: number, shown: number, total: number): string {
  if (total === 0) return "No events";
  return `Events ${offset + 1}–${offset + shown} of ${total}`;
}

export function hasNext(offset: number, shown: number, total: number): boolean {
  return offset + shown < total;
}

export function hasPrevious(offset: number): boolean {
  return offset > 0;
}

export function previousOffset(offset: number, size = AUDIT_PAGE_SIZE): number {
  return Math.max(0, offset - size);
}

/** What an event says about its artifact: `TMDLModel v3`. */
export function eventArtifact(e: AuditEvent): string {
  if (!e.artifact) return "";
  return e.version ? `${e.artifact} v${e.version}` : e.artifact;
}

/** A profile as short lines of evidence: rows, nulls, distinct values, range. */
export function evidenceLines(p: ProfileEvidence | null): string[] {
  if (!p) return [];
  const lines: string[] = [];
  if (p.row_count != null) lines.push(`${p.row_count.toLocaleString("en-US")} rows`);
  if (p.null_count != null) lines.push(`${p.null_count.toLocaleString("en-US")} nulls`);
  if (p.distinct_count != null) lines.push(`${p.distinct_count.toLocaleString("en-US")} distinct values${p.approximate ? " (approximate)" : ""}`);
  if (p.min != null || p.max != null) lines.push(`range ${String(p.min ?? "?")} to ${String(p.max ?? "?")}`);
  if (p.is_key_candidate) lines.push(p.is_unique ? "unique key candidate" : "key candidate");
  if (p.note) lines.push(p.note);
  return lines;
}

/** The KPI definition as label and value pairs, skipping what the contract left empty. */
export function definitionRows(kpi: TraceResult["kpi"]): [string, string][] {
  const d = kpi?.definition;
  if (!d) return [];
  const rows: [string, string][] = [["Definition", d.formula_intent ?? ""], ["Grain", d.grain ?? ""], ["Time logic", d.time_logic ?? ""], ["Currency", d.currency ?? ""]];
  if (d.filters?.length) rows.push(["Filters", d.filters.join("; ")]);
  if (d.exclusions?.length) rows.push(["Exclusions", d.exclusions.join("; ")]);
  return rows.filter(([, v]) => v !== "");
}

/** The steps of a trace, in order, for the breadcrumb: where each one leads. */
export function traceSteps(t: TraceResult): string[] {
  return [
    `Visual ${t.visual.id}: ${t.visual.title}`,
    t.measure ? `Measure ${t.measure.name}` : "No measure",
    t.measure?.dax ? "DAX" : "DAX missing",
    t.kpi ? `Approved KPI (contract v${t.kpi.contract_version ?? "?"})` : "No KPI",
    t.sources.length ? `${t.sources.length} source column${t.sources.length === 1 ? "" : "s"}` : "No source columns",
    "Prompt",
  ];
}
