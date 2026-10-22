// Display rules for the Model review screen (T-507). They only read what the API returned:
// whether the model may be approved is decided by the API (Gate G4, roles, the model hash),
// never here.

import type { GateResult, Me, ModelReview, ModelReviewMeasure, ModelReviewReconciliation, RoleStatus } from "./types.ts";

export const MODEL_ARTIFACT = "SemanticModel";
export const OWNER_ROLE = "business_owner";
export const MODEL_STALE_MESSAGE = "The model changed. Reload to see the latest version.";

const ROLE_LABELS: Record<string, string> = {
  bi_engineer: "BI engineer",
  business_owner: "Business owner",
  security_reviewer: "Security reviewer",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** What each role checks, shown next to its actions. */
export function roleScope(role: string): string {
  switch (role) {
    case "bi_engineer":
      return "Technical correctness: tables, relationships, DAX and the validation checks.";
    case "business_owner":
      return "Business fidelity: each measure implements the approved definition, without redefining it.";
    case "security_reviewer":
      return "Row-level security: each role's filter and who it exposes.";
    default:
      return "";
  }
}

export function failedChecks(gate: GateResult | null): string[] {
  return gate ? gate.checks.filter((c) => c.status === "fail").map((c) => c.id) : [];
}

/** One line for a role's approval state. */
export function statusLabel(s: RoleStatus): string {
  switch (s.status) {
    case "approved":
      return `Approved by ${s.approval?.user.upn ?? "unknown"}`;
    case "rejected":
      return `Rejected by ${s.approval?.user.upn ?? "unknown"}`;
    case "invalidated":
      return "Invalidated: model changed";
    default:
      return "Pending";
  }
}

export function statusColor(s: RoleStatus["status"]): "success" | "danger" | "warning" | "informative" {
  return s === "approved" ? "success" : s === "rejected" ? "danger" : s === "invalidated" ? "warning" : "informative";
}

function statusOf(review: ModelReview | null, role: string): RoleStatus | null {
  return review?.approvals_status.find((s) => s.role === role) ?? null;
}

/** Why the user can't approve as `role`, or null if they can. Each role is its own action. */
export function approveBlockedReason(review: ModelReview | null, me: Me | null, role: string): string | null {
  if (!review) return "The model hasn't been generated yet.";
  if (review.state === "MODEL_APPROVED") return "The model is approved.";
  if (review.state !== "MODEL_GENERATING") return `A run in ${review.state} can't approve the model.`;
  if (!me || !me.roles.includes(role)) return `Only a ${role} can approve as ${role}.`;
  if (statusOf(review, role)?.status === "approved") return "Already approved on this version of the model.";
  if (!review.gate.passed) return `Gate G4 failed: ${failedChecks(review.gate).join(", ")}.`;
  return null;
}

export function canApproveAs(review: ModelReview | null, me: Me | null, role: string): boolean {
  return approveBlockedReason(review, me, role) === null;
}

/** Reject is open to the role's holder while the run is MODEL_GENERATING. */
export function canRejectAs(review: ModelReview | null, me: Me | null, role: string): boolean {
  return !!review && review.state === "MODEL_GENERATING" && !!me && me.roles.includes(role);
}

/** Roles of the user that the model still needs. */
export function actableRoles(review: ModelReview | null, me: Me | null): string[] {
  return (review?.approvals_required ?? []).filter((role) => canRejectAs(review, me, role));
}

export function isOwner(role: string): boolean {
  return role === OWNER_ROLE;
}

/** The approved definition of a KPI, one line per part that is set. */
export function definitionLines(measure: ModelReviewMeasure): string[] {
  const kpi = measure.kpi_definition;
  if (!kpi) return ["The KPI of this measure isn't in the approved contract."];
  const lines = [kpi.formula_intent ? `Formula intent: ${kpi.formula_intent}` : "No formula intent."];
  if (kpi.grain) lines.push(`Grain: ${kpi.grain}`);
  if (kpi.filters.length > 0) lines.push(`Filters: ${kpi.filters.join("; ")}`);
  if (kpi.time_logic) lines.push(`Time logic: ${kpi.time_logic}`);
  if (kpi.exclusions.length > 0) lines.push(`Exclusions: ${kpi.exclusions.join("; ")}`);
  if (kpi.currency) lines.push(`Currency: ${kpi.currency}`);
  return lines;
}

export function formatValue(value: number | null): string {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(value);
}

/** The values to show next to a measure, as label and value pairs. */
export function reconciliationRows(r: ModelReviewReconciliation): { label: string; value: string }[] {
  if (r.status === "not_validated") return [{ label: "Reconciliation", value: "Not validated" }];
  return [
    { label: "DAX", value: formatValue(r.dax_value) },
    { label: "SQL", value: formatValue(r.sql_value) },
    { label: "Expected", value: r.expected === null ? "No business example" : formatValue(r.expected) },
  ];
}

export function reconciliationColor(status: ModelReviewReconciliation["status"]): "success" | "danger" | "warning" | "informative" {
  return status === "pass" ? "success" : status === "fail" ? "danger" : status === "warning" ? "warning" : "informative";
}

/** How a relationship filters, in words. */
export function filterDirection(crossFilter: string): string {
  return crossFilter === "both" ? "filters both ways" : "filters one way";
}

/** Whether the latest validation is of a different version of the model (validate again). */
export function needsValidation(review: ModelReview | null): boolean {
  return !!review && !review.validation_current;
}
