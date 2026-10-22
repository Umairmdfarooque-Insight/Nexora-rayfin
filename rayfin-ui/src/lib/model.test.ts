// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  actableRoles,
  approveBlockedReason,
  canApproveAs,
  canRejectAs,
  definitionLines,
  failedChecks,
  filterDirection,
  formatValue,
  needsValidation,
  reconciliationRows,
  roleLabel,
  statusLabel,
} from "./model.ts";
import type { ApprovalRecord, GateResult, Me, ModelReview, ModelReviewMeasure, RoleStatus } from "./types.ts";

function gate(passed: boolean, failed: string[] = []): GateResult {
  return {
    gate: "G4",
    artifact: "SemanticModel",
    artifact_hash: "sha256:m",
    passed,
    checked_at: "2026-10-16T00:00:00Z",
    checks: failed.map((id) => ({ id, description: id, status: "fail" as const, evidence: {} })),
  };
}

function record(role: string, action: "ACCEPT" | "REJECT" = "ACCEPT"): ApprovalRecord {
  return {
    approval_id: "a",
    run_id: "r",
    artifact: "SemanticModel",
    artifact_version: 2,
    artifact_hash: "sha256:m",
    action,
    role,
    user: { oid: "o", upn: `${role}@contoso.com` },
    at: "2026-10-16T00:00:00Z",
    comment: null,
    gate_result_ref: null,
  };
}

function review(overrides: Partial<ModelReview> = {}): ModelReview {
  const status: RoleStatus[] = [
    { role: "bi_engineer", status: "pending", approval: null, note: null },
    { role: "business_owner", status: "pending", approval: null, note: null },
  ];
  return {
    model_hash: "sha256:m",
    version: 2,
    state: "MODEL_GENERATING",
    tables: [],
    relationships: [],
    measures: [],
    roles: [],
    validation_report: null,
    validation_current: true,
    gate: gate(true),
    approvals_required: ["bi_engineer", "business_owner"],
    approvals_status: status,
    approvals_done: [],
    ...overrides,
  };
}

const me = (...roles: string[]): Me => ({ oid: "o", upn: "u@contoso.com", name: "U", roles });

function measure(overrides: Partial<ModelReviewMeasure> = {}): ModelReviewMeasure {
  return {
    name: "Net Revenue",
    table: "Sales",
    dax: "SUM(Sales[LineTotal])",
    description: null,
    kpi_definition: {
      name: "Net Revenue",
      formula_intent: "Sum of line totals excluding cancelled orders",
      grain: "order line",
      filters: [],
      time_logic: null,
      exclusions: ["Cancelled orders"],
      currency: "USD",
    },
    reconciliation: { dax_value: 1000, sql_value: 1000, expected: null, status: "pass", message: null },
    ...overrides,
  };
}

test("approving is open to a holder of the role while the model is being generated", () => {
  assert.equal(canApproveAs(review(), me("bi_engineer"), "bi_engineer"), true);
  assert.equal(approveBlockedReason(review(), me("bi_engineer"), "business_owner"), "Only a business_owner can approve as business_owner.");
  assert.equal(approveBlockedReason(null, me("bi_engineer"), "bi_engineer"), "The model hasn't been generated yet.");
});

test("a user with two roles can approve as either, one at a time", () => {
  const both = me("bi_engineer", "business_owner");
  const r = review({
    approvals_status: [
      { role: "bi_engineer", status: "approved", approval: record("bi_engineer"), note: null },
      { role: "business_owner", status: "pending", approval: null, note: null },
    ],
  });
  assert.equal(approveBlockedReason(r, both, "bi_engineer"), "Already approved on this version of the model.");
  assert.equal(canApproveAs(r, both, "business_owner"), true);
});

test("an invalidated approval can be given again", () => {
  const r = review({ approvals_status: [{ role: "bi_engineer", status: "invalidated", approval: record("bi_engineer"), note: "invalidated: model changed" }] });
  assert.equal(canApproveAs(r, me("bi_engineer"), "bi_engineer"), true);
  assert.equal(statusLabel(r.approvals_status[0]), "Invalidated: model changed");
});

test("a failing Gate G4 blocks approving, naming the checks, but not rejecting", () => {
  const r = review({ gate: gate(false, ["G4.RECON", "G4.DAX"]) });
  assert.equal(approveBlockedReason(r, me("bi_engineer"), "bi_engineer"), "Gate G4 failed: G4.RECON, G4.DAX.");
  assert.deepEqual(failedChecks(r.gate), ["G4.RECON", "G4.DAX"]);
  assert.equal(canRejectAs(r, me("bi_engineer"), "bi_engineer"), true);
});

test("other states can't approve", () => {
  assert.equal(approveBlockedReason(review({ state: "MODEL_APPROVED" }), me("bi_engineer"), "bi_engineer"), "The model is approved.");
  assert.equal(approveBlockedReason(review({ state: "MODEL_FAILED" }), me("bi_engineer"), "bi_engineer"), "A run in MODEL_FAILED can't approve the model.");
  assert.equal(canRejectAs(review({ state: "MODEL_FAILED" }), me("bi_engineer"), "bi_engineer"), false);
});

test("the roles the user can act as are the required ones they hold", () => {
  assert.deepEqual(actableRoles(review(), me("business_owner", "security_reviewer")), ["business_owner"]);
  assert.deepEqual(actableRoles(review({ approvals_required: ["bi_engineer", "business_owner", "security_reviewer"] }), me("security_reviewer")), ["security_reviewer"]);
  assert.deepEqual(actableRoles(review(), null), []);
});

test("status labels name who decided", () => {
  assert.equal(statusLabel({ role: "bi_engineer", status: "approved", approval: record("bi_engineer"), note: null }), "Approved by bi_engineer@contoso.com");
  assert.equal(statusLabel({ role: "bi_engineer", status: "rejected", approval: record("bi_engineer", "REJECT"), note: null }), "Rejected by bi_engineer@contoso.com");
  assert.equal(statusLabel({ role: "bi_engineer", status: "pending", approval: null, note: null }), "Pending");
  assert.equal(roleLabel("security_reviewer"), "Security reviewer");
  assert.equal(roleLabel("unknown_role"), "unknown_role");
});

test("the definition is shown next to the measure, one line per part", () => {
  assert.deepEqual(definitionLines(measure()), [
    "Formula intent: Sum of line totals excluding cancelled orders",
    "Grain: order line",
    "Exclusions: Cancelled orders",
    "Currency: USD",
  ]);
  assert.deepEqual(definitionLines(measure({ kpi_definition: null })), ["The KPI of this measure isn't in the approved contract."]);
});

test("the reconciliation values are labelled, with the missing ones said so", () => {
  assert.deepEqual(reconciliationRows(measure().reconciliation), [
    { label: "DAX", value: "1,000" },
    { label: "SQL", value: "1,000" },
    { label: "Expected", value: "No business example" },
  ]);
  assert.deepEqual(reconciliationRows({ dax_value: null, sql_value: null, expected: null, status: "not_validated", message: null }), [
    { label: "Reconciliation", value: "Not validated" },
  ]);
  assert.equal(formatValue(null), "—");
  assert.equal(formatValue(1234.56789), "1,234.5679");
});

test("filter directions and stale validation", () => {
  assert.equal(filterDirection("both"), "filters both ways");
  assert.equal(filterDirection("single"), "filters one way");
  assert.equal(needsValidation(review({ validation_current: false })), true);
  assert.equal(needsValidation(review()), false);
  assert.equal(needsValidation(null), false);
});
