// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  chainWarning,
  definitionRows,
  eventArtifact,
  eventTypeColor,
  eventTypeLabel,
  evidenceLines,
  hasNext,
  hasPrevious,
  pageRange,
  previousOffset,
  shortHash,
  traceSteps,
} from "./audit.ts";
import type { ProfileEvidence, TraceResult } from "./types.ts";

function trace(over: Partial<TraceResult> = {}): TraceResult {
  return {
    visual: { id: "v1", title: "Net Revenue by Month", type: "lineChart", page: "Overview", questions: [] },
    measure: { name: "Net Revenue", dax: "SUM(Sales[LineTotal])", model_version: 3 },
    other_measures: [],
    kpi: {
      definition: { name: "Net Revenue", formula_intent: "Sum of line totals", grain: "order line", filters: [], time_logic: null, exclusions: ["Cancelled orders"], currency: "USD" },
      contract_version: 4,
      approved_by: "a@b.com",
      approved_at: "2026-10-01T09:00:00Z",
    },
    sources: [{ table: "saleslt.T", column: "LineTotal", need: "amount", role: "measure", profile_evidence: null }],
    prompt: "Show net revenue",
    gaps: [],
    ...over,
  };
}

test("event types have labels and colours; an unknown type is shown as it is", () => {
  assert.equal(eventTypeLabel("approval"), "Approval");
  assert.equal(eventTypeLabel("novel"), "novel");
  assert.equal(eventTypeColor("approval"), "success");
  assert.equal(eventTypeColor("return"), "warning");
  assert.equal(eventTypeColor("state"), "brand");
});

test("a long hash is shortened and an empty one is empty", () => {
  assert.equal(shortHash("sha256:0123456789abcdef"), "sha256:0123456789…");
  assert.equal(shortHash(null), "");
});

test("an intact chain gives no warning; a broken one names the first broken event", () => {
  assert.equal(chainWarning({ chain_valid: true, first_broken_seq: null }), null);
  const w = chainWarning({ chain_valid: false, first_broken_seq: 4 });
  assert.match(w ?? "", /broken at event 4/);
  assert.match(chainWarning({ chain_valid: false, first_broken_seq: null }) ?? "", /broken\./);
});

test("paging helpers", () => {
  assert.equal(pageRange(0, 200, 251), "Events 1–200 of 251");
  assert.equal(pageRange(200, 51, 251), "Events 201–251 of 251");
  assert.equal(pageRange(0, 0, 0), "No events");
  assert.equal(hasNext(0, 200, 251), true);
  assert.equal(hasNext(200, 51, 251), false);
  assert.equal(hasPrevious(0), false);
  assert.equal(hasPrevious(200), true);
  assert.equal(previousOffset(200), 0);
  assert.equal(previousOffset(400), 200);
});

test("an event's artifact is shown with its version", () => {
  assert.equal(eventArtifact({ seq: 1, summary: "", artifact: "TMDLModel", version: 3 }), "TMDLModel v3");
  assert.equal(eventArtifact({ seq: 1, summary: "", artifact: "Release", version: null }), "Release");
  assert.equal(eventArtifact({ seq: 1, summary: "" }), "");
});

test("profile evidence is shown as short lines, and nothing without a profile", () => {
  assert.deepEqual(evidenceLines(null), []);
  const p: ProfileEvidence = {
    null_count: 3, distinct_count: 900, is_key_candidate: false, is_unique: null, min: 0.5, max: 9000, approximate: true,
    note: null, row_count: 1200, profiled_at: "x", queries: [],
  };
  assert.deepEqual(evidenceLines(p), ["1,200 rows", "3 nulls", "900 distinct values (approximate)", "range 0.5 to 9000"]);
});

test("the KPI definition skips what the contract left empty", () => {
  assert.deepEqual(definitionRows(trace().kpi), [
    ["Definition", "Sum of line totals"], ["Grain", "order line"], ["Currency", "USD"], ["Exclusions", "Cancelled orders"],
  ]);
  assert.deepEqual(definitionRows(null), []);
});

test("the trace breadcrumb names every step down to the prompt, and the gaps in it", () => {
  assert.deepEqual(traceSteps(trace()), [
    "Visual v1: Net Revenue by Month", "Measure Net Revenue", "DAX", "Approved KPI (contract v4)", "1 source column", "Prompt",
  ]);
  const bare = traceSteps(trace({ measure: null, kpi: null, sources: [] }));
  assert.deepEqual(bare.slice(1, 5), ["No measure", "DAX missing", "No KPI", "No source columns"]);
});
