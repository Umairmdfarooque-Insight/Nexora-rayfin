// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError } from "./api.ts";
import {
  acceptBlockedReason,
  canAccept,
  canChangeDraft,
  collapsed,
  COLLAPSE_AT,
  contractHash,
  describeError,
  failedChecks,
  formatList,
  isStale,
  parseList,
  STALE_MESSAGE,
} from "./review.ts";
import type { Contract, GateResult, Me, RunDetail } from "./types.ts";

const draft = { status: "draft" } as Contract;

function run(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: "r1",
    state: "CREATED",
    artifacts: {
      BusinessContract: { hash: "sha256:a", version: 1, path: "p", status: "draft", updated_at: "" },
    },
    contract: draft,
    ...over,
  } as RunDetail;
}

function gate(over: Partial<GateResult> = {}): GateResult {
  return {
    gate: "G1",
    artifact: "BusinessContract",
    artifact_hash: "sha256:a",
    passed: true,
    checked_at: "",
    checks: [{ id: "G1.1", description: "", status: "pass", evidence: {} }],
    ...over,
  };
}

const owner: Me = { oid: "o", upn: "u@x", name: "U", roles: ["business_owner"] };
const nobody: Me = { oid: "o", upn: "u@x", name: "U", roles: [] };

test("the hash comes from the BusinessContract artifact", () => {
  assert.equal(contractHash(run()), "sha256:a");
  assert.equal(contractHash(run({ artifacts: {} })), null);
  assert.equal(contractHash(null), null);
});

test("a stale version gets the fixed message, other errors keep theirs", () => {
  const stale = new ApiError(409, "STALE_VERSION", "server text");
  assert.ok(isStale(stale));
  assert.equal(describeError(stale), STALE_MESSAGE);
  assert.equal(describeError(new ApiError(403, "FORBIDDEN", "Only an owner")), "Only an owner");
  assert.ok(!isStale(new ApiError(409, "GATE_FAILED", "x")));
});

test("drafts can change only in CREATED or NEEDS_CLARIFICATION", () => {
  assert.ok(canChangeDraft(run()));
  assert.ok(canChangeDraft(run({ state: "NEEDS_CLARIFICATION" })));
  assert.ok(!canChangeDraft(run({ state: "REQUIREMENT_APPROVED" })));
  assert.ok(!canChangeDraft(run({ contract: { status: "approved" } as Contract })));
  assert.ok(!canChangeDraft(null));
});

test("Accept needs the owner, a passing gate on this hash, and CREATED", () => {
  assert.ok(canAccept(run(), gate(), owner));
  assert.ok(!canAccept(run(), gate({ passed: false }), owner));
  assert.ok(!canAccept(run(), gate(), nobody));
  assert.ok(!canAccept(run(), null, owner));
  assert.ok(!canAccept(run(), gate({ artifact_hash: "sha256:old" }), owner));
  assert.ok(!canAccept(run({ state: "NEEDS_CLARIFICATION" }), gate(), owner));
  assert.ok(!canAccept(run({ contract: { status: "approved" } as Contract }), gate(), owner));
});

test("the blocked reason names the failing checks and the missing role", () => {
  const failing = gate({
    passed: false,
    checks: [
      { id: "G1.2", description: "", status: "fail", evidence: {} },
      { id: "G1.5", description: "", status: "warning", evidence: {} },
    ],
  });
  assert.deepEqual(failedChecks(failing), ["G1.2"]);
  assert.match(acceptBlockedReason(run(), failing, owner) ?? "", /G1\.2/);
  assert.match(acceptBlockedReason(run(), gate(), nobody) ?? "", /business_owner/);
  assert.equal(acceptBlockedReason(run(), gate(), owner), null);
  assert.match(acceptBlockedReason(run({ contract: null }), null, owner) ?? "", /Compile/);
});

test("long text collapses and expands", () => {
  const long = "x".repeat(COLLAPSE_AT + 50);
  assert.equal(collapsed("short", false).truncated, false);
  const shown = collapsed(long, false);
  assert.ok(shown.truncated && shown.text.length < long.length && shown.text.endsWith("…"));
  assert.deepEqual(collapsed(long, true), { text: long, truncated: false });
});

test("lists round trip through comma-separated text", () => {
  assert.deepEqual(parseList(" a, b ,, c "), ["a", "b", "c"]);
  assert.deepEqual(parseList(""), []);
  assert.equal(formatList(["a", "b"]), "a, b");
});
