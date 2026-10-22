// Display rules for clarification and contract review. They only read what the API
// returned: whether a run may be approved is decided by the API (Gate G1), never here.

import type { ApiError } from "./api.ts";
import type { Contract, GateResult, Me, RunDetail } from "./types.ts";

export const OWNER_ROLE = "business_owner";
export const CONTRACT_ARTIFACT = "BusinessContract";
const EDITABLE_STATES = ["CREATED", "NEEDS_CLARIFICATION"];

export const STALE_MESSAGE = "The contract changed. Reload to see the latest version.";

/** The hash every edit and approval must send as `expected_hash`. */
export function contractHash(run: RunDetail | null): string | null {
  return run?.artifacts?.[CONTRACT_ARTIFACT]?.hash ?? null;
}

export function isStale(error: unknown): boolean {
  return (error as ApiError | undefined)?.code === "STALE_VERSION";
}

/** The text to show for an API error. */
export function describeError(error: ApiError): string {
  if (error.code === "STALE_VERSION") {
    return STALE_MESSAGE;
  }
  return error.message;
}

export function isLocked(contract: Contract | null): boolean {
  return contract?.status === "approved";
}

/** Answers, dismissals, edits and compiles are only possible on a draft in these states. */
export function canChangeDraft(run: RunDetail | null): boolean {
  return !!run && EDITABLE_STATES.includes(run.state) && !isLocked(run.contract);
}

export function hasRole(me: Me | null, role: string): boolean {
  return !!me && me.roles.includes(role);
}

export function failedChecks(gate: GateResult | null): string[] {
  return gate ? gate.checks.filter((c) => c.status === "fail").map((c) => c.id) : [];
}

/** Accept needs a passing G1 on the hash the user is looking at. */
export function canAccept(
  run: RunDetail | null,
  gate: GateResult | null,
  me: Me | null,
): boolean {
  return (
    canChangeDraft(run) &&
    run?.state === "CREATED" &&
    hasRole(me, OWNER_ROLE) &&
    gate !== null &&
    gate.passed &&
    gate.artifact_hash === contractHash(run)
  );
}

/** Why Accept is unavailable, or null if it is available. */
export function acceptBlockedReason(
  run: RunDetail | null,
  gate: GateResult | null,
  me: Me | null,
): string | null {
  if (!run || !run.contract) return "There is no contract yet. Compile the requirement first.";
  if (isLocked(run.contract)) return "The contract is approved and locked.";
  if (!canChangeDraft(run)) return `A run in ${run.state} can't be approved.`;
  if (!hasRole(me, OWNER_ROLE)) return `Only a ${OWNER_ROLE} can accept or reject the contract.`;
  if (!gate) return "Gate G1 hasn't been checked yet.";
  if (!gate.passed) return `Gate G1 failed: ${failedChecks(gate).join(", ")}.`;
  if (gate.artifact_hash !== contractHash(run)) {
    return "Gate G1 must be checked again for the latest version.";
  }
  if (run.state !== "CREATED") return "Answer the open questions first.";
  return null;
}

/** Long text is shown collapsed past this many characters. */
export const COLLAPSE_AT = 280;

export function collapsed(
  text: string,
  expanded: boolean,
): { text: string; truncated: boolean } {
  if (expanded || text.length <= COLLAPSE_AT) return { text, truncated: false };
  return { text: `${text.slice(0, COLLAPSE_AT).trimEnd()}…`, truncated: true };
}

/** Comma-separated input to a clean list, and back. */
export function parseList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function formatList(items: string[]): string {
  return items.join(", ");
}
