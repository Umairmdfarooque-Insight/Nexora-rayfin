// Display rules for the Report review screen (T-604). They only read what the API returned:
// whether the report may be approved is decided by the API (Gate G5, roles, the report hash),
// never here.

import type {
  CheckStatus,
  GateResult,
  Me,
  ReportPageReview,
  ReportReview,
  ReportVisualReview,
  RoleStatus,
} from "./types.ts";

export const REPORT_ARTIFACT = "Report";
export const REPORT_STALE_MESSAGE = "The report changed. Reload to see the latest version.";

const ROLE_LABELS: Record<string, string> = {
  project_reviewer: "Project reviewer",
  business_owner: "Business owner",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** What each role looks at, shown next to its actions. */
export function roleScope(role: string): string {
  switch (role) {
    case "project_reviewer":
      return "Usefulness and readiness: pages, visuals, interactions and the way the data shows.";
    case "business_owner":
      return "Business fit: each visual answers its question with the approved definitions.";
    default:
      return "";
  }
}

export function failedChecks(gate: GateResult | null): string[] {
  return gate ? gate.checks.filter((c) => c.status === "fail").map((c) => c.id) : [];
}

export function statusLabel(s: RoleStatus): string {
  switch (s.status) {
    case "approved":
      return `Approved by ${s.approval?.user.upn ?? "unknown"}`;
    case "rejected":
      return `Rejected by ${s.approval?.user.upn ?? "unknown"}`;
    case "invalidated":
      return "Invalidated: report changed";
    default:
      return "Pending";
  }
}

export function statusColor(s: RoleStatus["status"]): "success" | "danger" | "warning" | "informative" {
  return s === "approved" ? "success" : s === "rejected" ? "danger" : s === "invalidated" ? "warning" : "informative";
}

function statusOf(review: ReportReview | null, role: string): RoleStatus | null {
  return review?.approvals_status.find((s) => s.role === role) ?? null;
}

/** Why the user can't approve as `role`, or null if they can. Each role is its own action. */
export function approveBlockedReason(review: ReportReview | null, me: Me | null, role: string): string | null {
  if (!review) return "The report hasn't been generated yet.";
  if (review.state === "REPORT_APPROVED") return "The report is approved.";
  if (review.state !== "REPORT_GENERATING") return `A run in ${review.state} can't approve the report.`;
  if (!me || !me.roles.includes(role)) return `Only a ${role} can approve as ${role}.`;
  if (statusOf(review, role)?.status === "approved") return "Already approved on this version of the report.";
  if (!review.gate.passed) return `Gate G5 failed: ${failedChecks(review.gate).join(", ")}.`;
  return null;
}

export function canApproveAs(review: ReportReview | null, me: Me | null, role: string): boolean {
  return approveBlockedReason(review, me, role) === null;
}

/** Reject is open to the role's holder while the run is REPORT_GENERATING. */
export function canRejectAs(review: ReportReview | null, me: Me | null, role: string): boolean {
  return !!review && review.state === "REPORT_GENERATING" && !!me && me.roles.includes(role);
}

/** Regenerating a part is open to a holder of either required role while the report is reviewed. */
export function canRegenerate(review: ReportReview | null, me: Me | null): boolean {
  return !!review && review.state === "REPORT_GENERATING" && !!me && review.approvals_required.some((r) => me.roles.includes(r));
}

/** The role to send with a regenerate: the first required role the user holds. */
export function regenerateRole(review: ReportReview | null, me: Me | null): string | undefined {
  return review?.approvals_required.find((r) => me?.roles.includes(r));
}

/** The target of a regenerate: a whole page, or one visual of it. */
export function regenerateTarget(page: ReportPageReview, visual?: ReportVisualReview): { page: string; visual?: string } {
  return visual ? { page: page.display_name ?? page.name, visual: visual.id } : { page: page.display_name ?? page.name };
}

export function targetLabel(target: { page?: string; visual?: string } | null | undefined): string {
  if (!target) return "";
  return [target.page ? `page ${target.page}` : null, target.visual ? `visual ${target.visual}` : null].filter(Boolean).join(", ");
}

export function pageTitle(page: ReportPageReview): string {
  return page.display_name ?? page.name;
}

export function checkLabel(status: CheckStatus): string {
  switch (status) {
    case "pass":
      return "Passed";
    case "fail":
      return "Failed";
    case "warning":
      return "Warning";
    case "skipped":
      return "Skipped";
    default:
      return "Not validated";
  }
}

export function checkColor(status: CheckStatus): "success" | "danger" | "warning" | "informative" {
  return status === "pass" ? "success" : status === "fail" ? "danger" : status === "warning" ? "warning" : "informative";
}

/** What to show in place of a page image that wasn't rendered, or null if there is an image. */
export function renderPlaceholder(page: ReportPageReview): { text: string; detail: string | null; href: string | null } | null {
  if (page.image_url) return null;
  return {
    text: "Render unavailable — open in Power BI",
    detail: page.render_status === "not_validated" ? "The report hasn't been validated yet." : page.render_message,
    href: page.open_url,
  };
}

/** Whether the latest validation is of a different version of the report (validate again). */
export function needsValidation(review: ReportReview | null): boolean {
  return !!review && !review.validation_current;
}

/** The questions of a page's visuals, each listed once. */
export function pageQuestions(page: ReportPageReview): { id: string; text: string }[] {
  const seen = new Map<string, string>();
  for (const v of page.visuals) for (const q of v.questions) if (!seen.has(q.id)) seen.set(q.id, q.text);
  return [...seen].map(([id, text]) => ({ id, text }));
}
