// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  acceptBlockedReason,
  blockingActions,
  canAcceptPlan,
  canEditPlan,
  canOverride,
  canRejectPlan,
  checkDetails,
  evidenceFor,
  evidenceSummary,
  failedChecks,
  overrideRelationship,
  planApprovals,
  planHash,
  rejectedTables,
  removeMeasure,
  removeRelationship,
  setStorageMode,
  tableRationale,
  unresolvedEntities,
} from "./plan.ts";
import type {
  ExecutionPlan,
  GateResult,
  MetadataInventory,
  Me,
  ModelRelationship,
  ModelTable,
  RelationshipEvidence,
  RunDetail,
  SourceResolution,
} from "./types.ts";

function run(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: "r1",
    state: "DATA_PROFILED",
    artifacts: { ExecutionPlan: { hash: "sha256:p", version: 1, path: "p", status: "draft", updated_at: "" } },
    ...over,
  } as RunDetail;
}

function gate(over: Partial<GateResult> = {}): GateResult {
  return { gate: "G3", artifact: "ExecutionPlan", artifact_hash: "sha256:p", passed: true, checked_at: "", checks: [], ...over };
}

const engineer = { roles: ["bi_engineer"] } as Me;
const owner = { roles: ["business_owner"] } as Me;

test("the plan hash comes from the run's artifacts", () => {
  assert.equal(planHash(run()), "sha256:p");
  assert.equal(planHash(run({ artifacts: {} })), null);
  assert.equal(planHash(null), null);
});

test("editing needs a plan and the DATA_PROFILED state", () => {
  assert.equal(canEditPlan(run()), true);
  assert.equal(canEditPlan(run({ state: "PLAN_APPROVED" })), false);
  assert.equal(canEditPlan(run({ artifacts: {} })), false);
});

test("Accept needs a bi_engineer and a passing G3 on the current hash", () => {
  assert.equal(canAcceptPlan(run(), gate(), engineer), true);
  assert.equal(canAcceptPlan(run(), gate(), owner), false);
  assert.equal(canAcceptPlan(run(), gate({ passed: false }), engineer), false);
  assert.equal(canAcceptPlan(run(), gate({ artifact_hash: "sha256:old" }), engineer), false);
  assert.equal(canAcceptPlan(run(), null, engineer), false);
  assert.equal(canAcceptPlan(run({ state: "PLAN_APPROVED" }), gate(), engineer), false);
});

test("the reason Accept is unavailable is specific", () => {
  assert.match(acceptBlockedReason(run({ artifacts: {} }), null, engineer) ?? "", /No plan/);
  assert.match(acceptBlockedReason(run({ state: "PLAN_APPROVED" }), gate(), engineer) ?? "", /approved/);
  assert.match(acceptBlockedReason(run({ state: "CREATED" }), gate(), engineer) ?? "", /CREATED/);
  assert.match(acceptBlockedReason(run(), gate(), owner) ?? "", /bi_engineer/);
  assert.match(acceptBlockedReason(run(), null, engineer) ?? "", /hasn't been checked/);
  const failing = gate({
    passed: false,
    checks: [
      { id: "G3.6", description: "", status: "fail", evidence: {} },
      { id: "G3.1", description: "", status: "pass", evidence: {} },
    ],
  });
  assert.equal(acceptBlockedReason(run(), failing, engineer), "Gate G3 failed: G3.6.");
  assert.deepEqual(failedChecks(failing), ["G3.6"]);
  assert.match(acceptBlockedReason(run(), gate({ artifact_hash: "x" }), engineer) ?? "", /again/);
  assert.equal(acceptBlockedReason(run(), gate(), engineer), null);
});

test("Reject is open to a bi_engineer on a draft or an approved plan", () => {
  assert.equal(canRejectPlan(run(), engineer), true);
  assert.equal(canRejectPlan(run({ state: "PLAN_APPROVED" }), engineer), true);
  assert.equal(canRejectPlan(run({ state: "MODEL_GENERATING" }), engineer), false);
  assert.equal(canRejectPlan(run(), owner), false);
  assert.equal(canRejectPlan(run({ artifacts: {} }), engineer), false);
});

test("a failed check shows its paths, or its actions with the owner", () => {
  assert.deepEqual(
    checkDetails({ id: "G3.6", description: "", status: "fail", evidence: { paths: ["measures[Net Revenue]: needs Date"] } }),
    ["measures[Net Revenue]: needs Date"],
  );
  const details = checkDetails({
    id: "G3.5",
    description: "",
    status: "fail",
    evidence: { actions: [{ id: "DE1", kind: "create_date_table", description: "Create it", owner: null }] },
  });
  assert.equal(details[0], "DE1 create_date_table: Create it (owner: unassigned)");
  assert.deepEqual(checkDetails({ id: "G3.1", description: "", status: "pass", evidence: {} }), []);
  assert.deepEqual(
    checkDetails({ id: "x", description: "", status: "warning", evidence: { reason: "r" } }),
    ['{"reason":"r"}'],
  );
});

const relationship = (over: Partial<ModelRelationship> = {}): ModelRelationship => ({
  name: "Sales_Product",
  from: { table: "Sales", column: "ProductID" },
  to: { table: "Product", column: "ProductID" },
  cardinality: "many_to_one",
  cross_filter: "single",
  evidence_ref: "R1",
  override_rationale: null,
  ...over,
});

const evidence = (over: Partial<RelationshipEvidence> = {}): RelationshipEvidence =>
  ({
    id: "R1",
    from: { table: "a", column: "x" },
    to: { table: "b", column: "y" },
    confidence: "high",
    cardinality: "many_to_one",
    overlap_pct: 100,
    orphan_rate: 0,
    ...over,
  }) as RelationshipEvidence;

test("a relationship's evidence is found by its reference", () => {
  const inventory = { relationship_evidence: [evidence(), evidence({ id: "R2" })] } as MetadataInventory;
  assert.equal(evidenceFor(relationship(), inventory)?.id, "R1");
  assert.equal(evidenceFor(relationship({ evidence_ref: "R9" }), inventory), null);
  assert.equal(evidenceFor(relationship({ evidence_ref: null }), inventory), null);
  assert.equal(evidenceFor(relationship(), null), null);
});

test("the evidence summary names confidence, cardinality and the numbers", () => {
  assert.equal(
    evidenceSummary(relationship(), evidence()),
    "R1, high confidence, many to one, 100% overlap, 0% orphans",
  );
  assert.equal(evidenceSummary(relationship({ override_rationale: "Confirmed" }), null), "Override: Confirmed");
  assert.equal(evidenceSummary(relationship({ evidence_ref: null }), null), "No evidence");
  assert.equal(evidenceSummary(relationship({ evidence_ref: "R9" }), null), "R9: evidence not found");
});

const resolution = {
  entities: [
    {
      entity: "Product",
      selected: { table: "saleslt.SalesLT_Product", score: 0.9, conformed: true, exception_id: null, rationale: "Glossary match 'product'; conformed" },
      reason: null,
      rejected: [{ table: "nexora_test.dim_product_dupes", score: 0.5, reason: "Not conformed and no approved exception", requires_exception: true }],
    },
    { entity: "Campaign", selected: null, reason: "No table matches", rejected: [] },
  ],
  kpi_fields: [],
} as unknown as SourceResolution;

const table = (over: Partial<ModelTable> = {}): ModelTable => ({
  name: "Product",
  source: { schema: "saleslt", table: "SalesLT_Product" },
  role: "dimension",
  grain: null,
  columns: [],
  hierarchies: [],
  ...over,
});

test("a table's rationale comes from the entity it was selected for", () => {
  assert.equal(tableRationale(table(), resolution), "Product: Glossary match 'product'; conformed (score 0.9)");
  assert.equal(tableRationale(table({ source: { schema: "saleslt", table: "salesLT_product" } }), resolution).startsWith("Product:"), true);
  assert.match(tableRationale(table({ source: { schema: "s", table: "Header" } }), resolution), /KPI needs/);
  assert.match(tableRationale(table({ role: "date", source: { schema: "s", table: "d" } }), resolution), /time logic/);
  assert.match(tableRationale(table({ role: "bridge", source: { schema: "s", table: "b" } }), resolution), /bridge/);
  assert.equal(tableRationale(table({ source: null }), resolution), "No Lakehouse source.");
  assert.match(tableRationale(table(), null), /Holds fields/);
});

test("rejected alternatives carry their reason, and unresolved entities are listed", () => {
  assert.deepEqual(rejectedTables(resolution), [
    {
      entity: "Product",
      table: "nexora_test.dim_product_dupes",
      score: 0.5,
      reason: "Not conformed and no approved exception",
      requiresException: true,
    },
  ]);
  assert.deepEqual(unresolvedEntities(resolution), [{ entity: "Campaign", reason: "No table matches" }]);
  assert.deepEqual(rejectedTables(null), []);
});

test("only open blocking actions block generation", () => {
  const plan = {
    data_engineering_actions: [
      { id: "DE1", kind: "create_date_table", description: "", blocking: true, owner: null, status: "open" },
      { id: "DE2", kind: "create_bridge_table", description: "", blocking: true, owner: null, status: "done" },
      { id: "DE3", kind: "create_bridge_table", description: "", blocking: false, owner: null, status: "open" },
    ],
  } as ExecutionPlan;
  assert.deepEqual(blockingActions(plan).map((a) => a.id), ["DE1"]);
  assert.deepEqual(blockingActions(null), []);
});

test("edit operations have the shapes the API expects", () => {
  assert.deepEqual(removeRelationship("Sales_Product"), { op: "remove_relationship", name: "Sales_Product" });
  assert.deepEqual(removeMeasure("Net Revenue"), { op: "remove_measure", name: "Net Revenue" });
  assert.deepEqual(setStorageMode("Import"), { op: "set_storage_mode", value: "Import" });
  assert.deepEqual(overrideRelationship("Sales_Product", "  Confirmed  "), {
    op: "override_relationship",
    name: "Sales_Product",
    rationale: "Confirmed",
  });
  assert.equal(canOverride("  "), false);
  assert.equal(canOverride("why"), true);
});

test("only the plan's own approvals are listed", () => {
  const records = [{ artifact: "ExecutionPlan" }, { artifact: "MetadataInventory" }, { artifact: "ExecutionPlan" }];
  assert.equal(planApprovals(records).length, 2);
});
