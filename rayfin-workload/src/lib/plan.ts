// Display rules for the Plan review screen (T-405). They only read what the API returned:
// whether a plan may be accepted is decided by the API (Gate G3), never here.

import type {
  DataEngineeringAction,
  ExecutionPlan,
  GateCheck,
  GateResult,
  MetadataInventory,
  Me,
  ModelRelationship,
  ModelTable,
  PlanOp,
  RelationshipEvidence,
  RunDetail,
  SourceResolution,
} from "./types.ts";

export const PLAN_ARTIFACT = "ExecutionPlan";
export const PLAN_ROLE = "bi_engineer";
export const PLAN_STALE_MESSAGE = "The plan changed. Reload to see the latest version.";

/** The hash every edit and approval of the plan must send as `expected_hash`. */
export function planHash(run: RunDetail | null): string | null {
  return run?.artifacts?.[PLAN_ARTIFACT]?.hash ?? null;
}

export function hasPlanRole(me: Me | null): boolean {
  return !!me && me.roles.includes(PLAN_ROLE);
}

/** Edits, regeneration and Accept only happen while the run is DATA_PROFILED. */
export function canEditPlan(run: RunDetail | null): boolean {
  return run?.state === "DATA_PROFILED" && planHash(run) !== null;
}

export function failedChecks(gate: GateResult | null): string[] {
  return gate ? gate.checks.filter((c) => c.status === "fail").map((c) => c.id) : [];
}

/** Accept needs a bi_engineer and a passing G3 on the hash the user is looking at. */
export function canAcceptPlan(run: RunDetail | null, gate: GateResult | null, me: Me | null): boolean {
  return acceptBlockedReason(run, gate, me) === null;
}

/** Why Accept is unavailable, or null if it is available. */
export function acceptBlockedReason(
  run: RunDetail | null,
  gate: GateResult | null,
  me: Me | null,
): string | null {
  if (!run || !planHash(run)) return "No plan has been generated yet.";
  if (run.state === "PLAN_APPROVED") return "The plan is approved. Reject it to reopen it.";
  if (run.state !== "DATA_PROFILED") return `A run in ${run.state} can't accept the plan.`;
  if (!hasPlanRole(me)) return `Only a ${PLAN_ROLE} can accept or reject the plan.`;
  if (!gate) return "Gate G3 hasn't been checked yet.";
  if (!gate.passed) return `Gate G3 failed: ${failedChecks(gate).join(", ")}.`;
  if (gate.artifact_hash !== planHash(run)) return "Gate G3 must be checked again for the latest plan.";
  return null;
}

/** Reject is open to a bi_engineer on a draft or an approved plan. */
export function canRejectPlan(run: RunDetail | null, me: Me | null): boolean {
  return (
    !!run &&
    planHash(run) !== null &&
    (run.state === "DATA_PROFILED" || run.state === "PLAN_APPROVED") &&
    hasPlanRole(me)
  );
}

/** What a failed check found: the paths it names, or its evidence as text. */
export function checkDetails(check: GateCheck): string[] {
  const paths = check.evidence.paths;
  if (Array.isArray(paths)) return paths.map(String);
  const actions = check.evidence.actions;
  if (Array.isArray(actions)) {
    return actions.map((a) => {
      const action = a as DataEngineeringAction;
      return `${action.id} ${action.kind}: ${action.description} (owner: ${action.owner ?? "unassigned"})`;
    });
  }
  return Object.keys(check.evidence).length > 0 ? [JSON.stringify(check.evidence)] : [];
}

/** The measured evidence a relationship points at. */
export function evidenceFor(
  relationship: ModelRelationship,
  inventory: MetadataInventory | null,
): RelationshipEvidence | null {
  if (!relationship.evidence_ref || !inventory) return null;
  return inventory.relationship_evidence.find((e) => e.id === relationship.evidence_ref) ?? null;
}

/** One line of evidence for a relationship, or the reason there is none. */
export function evidenceSummary(
  relationship: ModelRelationship,
  evidence: RelationshipEvidence | null,
): string {
  if (relationship.override_rationale) return `Override: ${relationship.override_rationale}`;
  if (!evidence) return relationship.evidence_ref ? `${relationship.evidence_ref}: evidence not found` : "No evidence";
  const parts = [`${evidence.id}`, `${evidence.confidence ?? "unrated"} confidence`];
  if (evidence.cardinality) parts.push(evidence.cardinality.replace(/_/g, " "));
  if (evidence.overlap_pct !== null) parts.push(`${evidence.overlap_pct}% overlap`);
  if (evidence.orphan_rate !== null) parts.push(`${evidence.orphan_rate}% orphans`);
  return parts.join(", ");
}

/** Why a model table is in the plan, from the source resolution. */
export function tableRationale(table: ModelTable, resolution: SourceResolution | null): string {
  if (!table.source) return "No Lakehouse source.";
  const key = `${table.source.schema}.${table.source.table}`.toLowerCase();
  const entity = resolution?.entities.find((e) => e.selected?.table.toLowerCase() === key);
  if (entity?.selected) return `${entity.entity}: ${entity.selected.rationale} (score ${entity.selected.score})`;
  if (table.role === "date") return "Date table for the KPIs' time logic.";
  if (table.role === "bridge") return "Mapping table proposed as a bridge.";
  return "Holds fields that a KPI needs.";
}

export interface RejectedTable {
  entity: string;
  table: string;
  score: number;
  reason: string;
  requiresException: boolean;
}

/** The alternatives that were not chosen, with the reason, for every entity. */
export function rejectedTables(resolution: SourceResolution | null): RejectedTable[] {
  return (resolution?.entities ?? []).flatMap((e) =>
    e.rejected.map((r) => ({
      entity: e.entity,
      table: r.table,
      score: r.score,
      reason: r.reason,
      requiresException: r.requires_exception,
    })),
  );
}

/** Entities with no selected table, with the reason. */
export function unresolvedEntities(resolution: SourceResolution | null): { entity: string; reason: string }[] {
  return (resolution?.entities ?? [])
    .filter((e) => !e.selected)
    .map((e) => ({ entity: e.entity, reason: e.reason ?? "No source was selected." }));
}

/** Open actions that block generation (check G3.5). */
export function blockingActions(plan: ExecutionPlan | null): DataEngineeringAction[] {
  return (plan?.data_engineering_actions ?? []).filter((a) => a.blocking && a.status === "open");
}

export function removeRelationship(name: string): PlanOp {
  return { op: "remove_relationship", name };
}

export function removeMeasure(name: string): PlanOp {
  return { op: "remove_measure", name };
}

export function setStorageMode(value: "DirectLake" | "Import"): PlanOp {
  return { op: "set_storage_mode", value };
}

export function overrideRelationship(name: string, rationale: string): PlanOp {
  return { op: "override_relationship", name, rationale: rationale.trim() };
}

/** An override is only sent with a rationale. */
export function canOverride(rationale: string): boolean {
  return rationale.trim().length > 0;
}

/** Approvals of the plan (ExecutionPlan edits, accepts and rejects), oldest first. */
export function planApprovals<T extends { artifact: string }>(approvals: T[]): T[] {
  return approvals.filter((a) => a.artifact === PLAN_ARTIFACT);
}
