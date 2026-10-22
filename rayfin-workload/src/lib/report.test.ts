// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  approveBlockedReason,
  canApproveAs,
  canRegenerate,
  canRejectAs,
  checkLabel,
  failedChecks,
  needsValidation,
  pageQuestions,
  regenerateRole,
  regenerateTarget,
  renderPlaceholder,
  roleLabel,
  statusLabel,
  targetLabel,
} from "./report.ts";
import type { ApprovalRecord, GateResult, Me, ReportPageReview, ReportReview, RoleStatus } from "./types.ts";

function gate(passed: boolean, failed: string[] = []): GateResult {
  return {
    gate: "G5",
    artifact: "Report",
    artifact_hash: "sha256:r",
    passed,
    checked_at: "2026-10-16T00:00:00Z",
    checks: failed.map((id) => ({ id, description: id, status: "fail" as const, evidence: {} })),
  };
}

function record(role: string, action: "ACCEPT" | "REJECT" = "ACCEPT"): ApprovalRecord {
  return {
    approval_id: "a",
    run_id: "r",
    artifact: "Report",
    artifact_version: 1,
    artifact_hash: "sha256:r",
    action,
    role,
    user: { oid: "o", upn: `${role}@contoso.com` },
    at: "2026-10-16T00:00:00Z",
    comment: null,
    gate_result_ref: null,
  };
}

function page(overrides: Partial<ReportPageReview> = {}): ReportPageReview {
  return {
    name: "p1",
    display_name: "Overview",
    image_url: "/runs/r/files/report/v1/renders/p1.png",
    render_status: "pass",
    render_message: null,
    open_url: "https://app.powerbi.com/groups/w/reports/x",
    visuals: [
      {
        id: "v1",
        title: "Net Revenue by Month",
        type: "lineChart",
        questions: [{ id: "Q-R1", text: "How does Net Revenue trend?" }],
        bind_status: "pass",
        query_status: "pass",
        messages: [],
      },
      {
        id: "v2",
        title: "Net Revenue by Product",
        type: "clusteredBarChart",
        questions: [{ id: "Q-R1", text: "How does Net Revenue trend?" }, { id: "Q-R2", text: "How do the KPIs break down by Product?" }],
        bind_status: "pass",
        query_status: "warning",
        messages: ["v2: no data: the query returned no rows"],
      },
    ],
    ...overrides,
  };
}

function review(overrides: Partial<ReportReview> = {}): ReportReview {
  const status: RoleStatus[] = [
    { role: "project_reviewer", status: "pending", approval: null, note: null },
    { role: "business_owner", status: "pending", approval: null, note: null },
  ];
  return {
    report_hash: "sha256:r",
    version: 1,
    state: "REPORT_GENERATING",
    pages: [page()],
    unanswered_questions: [],
    validation_report: null,
    validation_current: true,
    gate: gate(true),
    approvals_required: ["project_reviewer", "business_owner"],
    approvals_status: status,
    approvals_done: [],
    ...overrides,
  };
}

const me = (...roles: string[]): Me => ({ oid: "o", upn: "u@contoso.com", name: "U", roles });

test("approving is open to a holder of the role while the report is being reviewed", () => {
  assert.equal(canApproveAs(review(), me("project_reviewer"), "project_reviewer"), true);
  assert.equal(approveBlockedReason(review(), me("project_reviewer"), "business_owner"), "Only a business_owner can approve as business_owner.");
  assert.equal(approveBlockedReason(null, me("project_reviewer"), "project_reviewer"), "The report hasn't been generated yet.");
});

test("a user with both roles approves one at a time", () => {
  const both = me("project_reviewer", "business_owner");
  const r = review({
    approvals_status: [
      { role: "project_reviewer", status: "approved", approval: record("project_reviewer"), note: null },
      { role: "business_owner", status: "pending", approval: null, note: null },
    ],
  });
  assert.equal(approveBlockedReason(r, both, "project_reviewer"), "Already approved on this version of the report.");
  assert.equal(canApproveAs(r, both, "business_owner"), true);
});

test("an invalidated approval can be given again", () => {
  const r = review({ approvals_status: [{ role: "project_reviewer", status: "invalidated", approval: record("project_reviewer"), note: "invalidated: report changed" }] });
  assert.equal(canApproveAs(r, me("project_reviewer"), "project_reviewer"), true);
  assert.equal(statusLabel(r.approvals_status[0]), "Invalidated: report changed");
});

test("a failing Gate G5 blocks approving, naming the checks, but not rejecting", () => {
  const r = review({ gate: gate(false, ["G5.BIND", "G5.CURRENT"]) });
  assert.equal(approveBlockedReason(r, me("project_reviewer"), "project_reviewer"), "Gate G5 failed: G5.BIND, G5.CURRENT.");
  assert.deepEqual(failedChecks(r.gate), ["G5.BIND", "G5.CURRENT"]);
  assert.equal(canRejectAs(r, me("project_reviewer"), "project_reviewer"), true);
});

test("other states can't approve, reject or regenerate", () => {
  assert.equal(approveBlockedReason(review({ state: "REPORT_APPROVED" }), me("project_reviewer"), "project_reviewer"), "The report is approved.");
  assert.equal(approveBlockedReason(review({ state: "REPORT_FAILED" }), me("project_reviewer"), "project_reviewer"), "A run in REPORT_FAILED can't approve the report.");
  assert.equal(canRejectAs(review({ state: "REPORT_FAILED" }), me("project_reviewer"), "project_reviewer"), false);
  assert.equal(canRegenerate(review({ state: "REPORT_FAILED" }), me("project_reviewer")), false);
});

test("regenerating is open to a holder of either required role", () => {
  assert.equal(canRegenerate(review(), me("business_owner")), true);
  assert.equal(canRegenerate(review(), me("data_engineer")), false);
  assert.equal(canRegenerate(review(), null), false);
  assert.equal(regenerateRole(review(), me("business_owner")), "business_owner");
  assert.equal(regenerateRole(review(), me("project_reviewer", "business_owner")), "project_reviewer");
});

test("the target of a regenerate is a page or one of its visuals", () => {
  const p = page();
  assert.deepEqual(regenerateTarget(p), { page: "Overview" });
  assert.deepEqual(regenerateTarget(p, p.visuals[0]), { page: "Overview", visual: "v1" });
  assert.deepEqual(regenerateTarget(page({ display_name: null })), { page: "p1" });
  assert.equal(targetLabel({ page: "Overview", visual: "v1" }), "page Overview, visual v1");
  assert.equal(targetLabel(null), "");
});

test("a page without a render shows a placeholder that links to Power BI", () => {
  assert.equal(renderPlaceholder(page()), null);
  const p = page({ image_url: null, render_status: "warning", render_message: "export unavailable: Capacity not supported" });
  assert.deepEqual(renderPlaceholder(p), {
    text: "Render unavailable — open in Power BI",
    detail: "export unavailable: Capacity not supported",
    href: "https://app.powerbi.com/groups/w/reports/x",
  });
  assert.equal(renderPlaceholder(page({ image_url: null, render_status: "not_validated" }))?.detail, "The report hasn't been validated yet.");
  assert.equal(renderPlaceholder(page({ image_url: null, open_url: null }))?.href, null);
});

test("a page lists each question its visuals answer once", () => {
  assert.deepEqual(pageQuestions(page()).map((q) => q.id), ["Q-R1", "Q-R2"]);
});

test("validation is needed when the latest one is of another version", () => {
  assert.equal(needsValidation(review({ validation_current: false })), true);
  assert.equal(needsValidation(review()), false);
  assert.equal(needsValidation(null), false);
});

test("labels", () => {
  assert.equal(roleLabel("project_reviewer"), "Project reviewer");
  assert.equal(roleLabel("other"), "other");
  assert.equal(checkLabel("not_validated"), "Not validated");
  assert.equal(statusLabel({ role: "business_owner", status: "rejected", approval: record("business_owner", "REJECT"), note: null }), "Rejected by business_owner@contoso.com");
  assert.equal(statusLabel({ role: "business_owner", status: "pending", approval: null, note: null }), "Pending");
});
