// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  acceptBlockedReason,
  candidateTables,
  canAcceptInventory,
  exceptionRecords,
  filterTables,
  formatPercent,
  inventoryHash,
  missingExceptions,
  paginate,
  PAGE_SIZE,
  parseSchemas,
  tableKey,
  tableWarnings,
  warningColumn,
  warningEvidence,
} from "./inventory.ts";
import type {
  ApprovalRecord,
  GateResult,
  InventoryTable,
  Me,
  RunDetail,
  TableProfile,
} from "./types.ts";

function run(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: "r1",
    state: "REQUIREMENT_APPROVED",
    artifacts: {
      MetadataInventory: { hash: "sha256:a", version: 1, path: "p", status: "draft", updated_at: "" },
    },
    ...over,
  } as RunDetail;
}

function gate(over: Partial<GateResult> = {}): GateResult {
  return {
    gate: "G2",
    artifact: "MetadataInventory",
    artifact_hash: "sha256:a",
    passed: true,
    checked_at: "",
    checks: [
      { id: "G2.1", description: "", status: "pass", evidence: { candidates: ["saleslt.SalesLT_Product"] } },
      { id: "G2.5", description: "", status: "fail", evidence: { missing_exceptions: ["saleslt.SalesLT_Customer"] } },
    ],
    ...over,
  };
}

const engineer = { roles: ["data_engineer"] } as Me;
const nobody = { roles: ["bi_engineer"] } as Me;

function table(name: string, over: Partial<InventoryTable> = {}): InventoryTable {
  return { schema: "saleslt", table_name: name, description: null, columns: [], profile: null, errors: [], ...over };
}

const profile: TableProfile = {
  status: "warning",
  code: null,
  message: null,
  row_count: 9,
  columns: {
    ProductID: {
      null_count: 1, distinct_count: 5, is_key_candidate: true, is_unique: false,
      duplicate_count: 3, min: 1, max: 5, approximate: false, note: null,
    },
  },
  freshness: null,
  warnings: ["ProductID is not unique (3 duplicate values)"],
  evidence: {
    queries: [
      "SELECT COUNT_BIG(*) FROM [nexora_test].[dim_product_dupes]",
      "SELECT COUNT_BIG(*) FROM (SELECT [ProductID] FROM [nexora_test].[dim_product_dupes] GROUP BY [ProductID] HAVING COUNT(*) > 1) AS d",
    ],
    profiled_at: "",
  },
};

test("inventoryHash reads the artifact hash", () => {
  assert.equal(inventoryHash(run()), "sha256:a");
  assert.equal(inventoryHash(run({ artifacts: {} })), null);
  assert.equal(inventoryHash(null), null);
});

test("filterTables searches the schema.table name, ignoring case", () => {
  const tables = [table("SalesLT_Product"), table("SalesLT_Customer"), { ...table("dim_date"), schema: "nexora_test" }];
  assert.deepEqual(filterTables(tables, "").length, 3);
  assert.deepEqual(filterTables(tables, "  CUSTOMER ").map(tableKey), ["saleslt.SalesLT_Customer"]);
  assert.deepEqual(filterTables(tables, "nexora_test.").map(tableKey), ["nexora_test.dim_date"]);
  assert.equal(filterTables(tables, "zzz").length, 0);
});

test("paginate pages a list of over 200 tables and clamps the page", () => {
  const items = Array.from({ length: 230 }, (_, i) => i);
  const first = paginate(items, 1);
  assert.equal(first.items.length, PAGE_SIZE);
  assert.equal(first.pages, 5);
  assert.deepEqual(paginate(items, 5).items.length, 30);
  assert.equal(paginate(items, 99).page, 5);
  assert.equal(paginate(items, -3).page, 1);
  assert.deepEqual(paginate([], 1), { items: [], page: 1, pages: 1 });
});

test("tableWarnings joins profile warnings and errors", () => {
  assert.deepEqual(tableWarnings(table("t")), []);
  const warned = table("t", { profile, errors: [{ code: "NO_COLUMNS", message: "no columns" }] });
  assert.deepEqual(tableWarnings(warned), ["ProductID is not unique (3 duplicate values)", "NO_COLUMNS: no columns"]);
  const failed = table("t", { profile: { ...profile, status: "error", code: "TABLE_NOT_FOUND", message: "gone", warnings: [] } });
  assert.deepEqual(tableWarnings(failed), ["TABLE_NOT_FOUND: gone"]);
});

test("warningColumn finds the column a warning is about", () => {
  const columns = [{ name: "Product" }, { name: "ProductID" }] as InventoryTable["columns"];
  assert.equal(warningColumn("ProductID is not unique (3 duplicate values)", columns), "ProductID");
  assert.equal(warningColumn("The table is empty", columns), null);
});

test("warningEvidence returns the queries that name the column and its numbers", () => {
  const evidence = warningEvidence(profile, "ProductID");
  assert.equal(evidence.queries.length, 1);
  assert.match(evidence.queries[0], /HAVING COUNT/);
  assert.equal(evidence.stats?.duplicate_count, 3);
  const whole = warningEvidence(profile, null);
  assert.equal(whole.queries.length, 2);
  assert.equal(whole.stats, null);
  assert.equal(warningEvidence(profile, "Other").queries.length, 2);
});

test("candidate tables and missing exceptions come from the gate evidence", () => {
  assert.deepEqual(candidateTables(gate()), ["saleslt.SalesLT_Product"]);
  assert.deepEqual(missingExceptions(gate()), ["saleslt.SalesLT_Customer"]);
  assert.deepEqual(candidateTables(null), []);
  assert.deepEqual(missingExceptions(null), []);
});

test("exceptionRecords keeps only exception approvals", () => {
  const records = [
    { artifact: "Exception:saleslt.SalesLT_Customer" },
    { artifact: "MetadataInventory" },
    { artifact: "BusinessContract" },
  ] as ApprovalRecord[];
  assert.equal(exceptionRecords(records).length, 1);
});

test("Accept needs a passing G2 on the current hash, the data role and the right state", () => {
  assert.equal(canAcceptInventory(run(), gate(), engineer), true);
  assert.equal(canAcceptInventory(run(), gate({ passed: false }), engineer), false);
  assert.equal(canAcceptInventory(run(), gate({ artifact_hash: "sha256:old" }), engineer), false);
  assert.equal(canAcceptInventory(run(), gate(), nobody), false);
  assert.equal(canAcceptInventory(run({ state: "DATA_PROFILED" }), gate(), engineer), false);
  assert.equal(canAcceptInventory(run(), null, engineer), false);
});

test("acceptBlockedReason explains why, or is null", () => {
  assert.equal(acceptBlockedReason(run(), gate(), engineer), null);
  assert.match(acceptBlockedReason(run({ artifacts: {} }), null, engineer) ?? "", /harvested/);
  assert.match(acceptBlockedReason(run({ state: "DATA_PROFILED" }), gate(), engineer) ?? "", /Re-harvest/);
  assert.match(acceptBlockedReason(run({ state: "CREATED" }), gate(), engineer) ?? "", /CREATED/);
  assert.match(acceptBlockedReason(run(), gate(), nobody) ?? "", /data_engineer/);
  assert.match(acceptBlockedReason(run(), null, engineer) ?? "", /hasn't been checked/);
  assert.match(acceptBlockedReason(run(), gate({ passed: false }), engineer) ?? "", /G2 failed: G2\.5/);
  assert.match(acceptBlockedReason(run(), gate({ artifact_hash: "sha256:old" }), engineer) ?? "", /again/);
});

test("parseSchemas and formatPercent", () => {
  assert.deepEqual(parseSchemas(" saleslt, nexora_test ,, "), ["saleslt", "nexora_test"]);
  assert.deepEqual(parseSchemas(""), []);
  assert.equal(formatPercent(12.5), "12.5%");
  assert.equal(formatPercent(null), "n/a");
});
