// Client for nexora_api (the UDF) and nexora_stage_runner (the Job Scheduler),
// replacing the first build's direct-FastAPI client (T-102 FR-2). It only
// fetches, reserves and polls: every decision (states, next actions,
// validation) still comes from the API. The pages keep calling `api.*` by
// operation name; long operations go through `runJob`, which reserves a job,
// starts the notebook, polls the run every 2s (then every 5s after 30s), and
// returns the job's result.
//
// Not yet confirmed against a live tenant (fabric-hosting.md's platform-limits
// table): the exact invocation envelope below matches Microsoft's published
// "invoke a user data function from an external application" tutorial
// (functionName/invocationId/status/output/errors), and the Job Scheduler
// body matches a community-confirmed `executionData.parameters` shape for
// `POST …/items/{id}/jobs/instances?jobType=RunNotebook`.

import { loadConfig } from "./config.ts";
import { getFabricHostClient } from "./fabricHost.ts";
import type {
  ApprovalBody,
  ApprovalRecord,
  ApprovalResult,
  AuditPage,
  BridgeGenerateResult,
  CompileResult,
  CreateRunBody,
  ExceptionResult,
  GateResult,
  HarvestResult,
  MeasuresResult,
  Me,
  MetadataInventory,
  ModelGenerateResult,
  ModelReview,
  ModelValidateResult,
  PlanEditResult,
  PlanOp,
  PlanResult,
  QuestionResult,
  RelationshipEvidence,
  RepairResult,
  ReportGenerateResult,
  ReportPlanResult,
  ReportReview,
  ReportValidateResult,
  RunDetail,
  RunFile,
  RunList,
  ScopeBody,
  SourceResolution,
  StageCounts,
  TraceResult,
} from "./types.ts";

const POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api";
const FABRIC_API = "https://api.fabric.microsoft.com/v1";

export class ApiError extends Error {
  status: number; // 0 when the API couldn't be reached
  code: string;
  fields: Record<string, string>;

  constructor(
    status: number,
    code: string,
    message: string,
    fields: Record<string, string> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }

  get unreachable(): boolean {
    return this.status === 0;
  }
}

/** Field-level messages from a 422 VALIDATION_ERROR envelope. */
function fieldMessages(details: unknown): Record<string, string> {
  const fields: Record<string, string> = {};
  const errors = (details as { errors?: unknown } | null)?.errors;
  if (Array.isArray(errors)) {
    for (const item of errors) {
      const { field, message } = item as { field?: string; message?: string };
      if (field && message && !(field in fields)) {
        fields[field] = message;
      }
    }
  }
  return fields;
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown };
}

interface DispatchEnvelope {
  status: number;
  body: unknown;
}

interface UdfInvocationResult {
  functionName: string;
  invocationId: string;
  status: "Succeeded" | "BadRequest" | "Failed" | "Timeout" | "ResponseTooLarge";
  output: DispatchEnvelope;
  errors: { name: string; message: string; properties?: Record<string, unknown> }[];
}

async function invokeFunction(
  functionUrl: string,
  args: Record<string, unknown>,
): Promise<DispatchEnvelope> {
  const host = getFabricHostClient();
  if (!host || !functionUrl) {
    throw new ApiError(0, "NETWORK", "The Fabric workload host isn't available.");
  }
  const token = await host.acquireAccessToken(POWERBI_RESOURCE);
  let response: Response;
  try {
    response = await fetch(functionUrl, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(args),
    });
  } catch {
    throw new ApiError(0, "NETWORK", `Can't reach nexora_api at ${functionUrl}.`);
  }
  if (!response.ok) {
    throw new ApiError(response.status, "UPSTREAM_ERROR", `nexora_api returned HTTP ${response.status}.`);
  }
  const result = (await response.json()) as UdfInvocationResult;
  if (result.status !== "Succeeded") {
    const detail = result.errors.map((e) => e.message).join("; ") || result.status;
    throw new ApiError(502, "UPSTREAM_ERROR", `nexora_api call failed: ${detail}`);
  }
  return result.output;
}

function raiseIfError(envelope: DispatchEnvelope): void {
  if (envelope.status >= 400) {
    const error = (envelope.body as ErrorEnvelope)?.error;
    throw new ApiError(
      envelope.status,
      error?.code ?? "UNKNOWN",
      error?.message ?? `nexora_api returned HTTP ${envelope.status}.`,
      fieldMessages(error?.details),
    );
  }
}

/** `call(method, path, …)`: the `call` function of `nexora_api`
 * (CONTRACTS.md#fabric-operations). Used directly for `udf:` operations; a
 * `job:` operation called this way gets `400 BAD_REQUEST` from the dispatcher
 * -- use `runJob` instead. */
export async function call<T>(
  method: string,
  path: string,
  query: Record<string, unknown> = {},
  body?: unknown,
): Promise<T> {
  const config = loadConfig();
  const envelope = await invokeFunction(config.nexoraApiCallFunctionUrl, {
    method,
    path,
    query,
    body: body ?? null,
  });
  raiseIfError(envelope);
  return envelope.body as T;
}

export async function me(): Promise<Me> {
  const config = loadConfig();
  const envelope = await invokeFunction(config.nexoraApiMeFunctionUrl, {});
  raiseIfError(envelope);
  return envelope.body as Me;
}

async function startStageRunnerJob(
  jobId: string,
  runId: string,
  operation: string,
  body: unknown,
): Promise<void> {
  const host = getFabricHostClient();
  const config = loadConfig();
  if (!host) {
    throw new ApiError(0, "NETWORK", "The Fabric workload host isn't available.");
  }
  const token = await host.acquireAccessToken(POWERBI_RESOURCE);
  const url =
    `${FABRIC_API}/workspaces/${encodeURIComponent(config.homeWorkspaceId)}` +
    `/items/${encodeURIComponent(config.stageRunnerItemId)}/jobs/instances?jobType=RunNotebook`;
  const payload = {
    executionData: {
      parameters: {
        job_id: { value: jobId, type: "string" },
        run_id: { value: runId, type: "string" },
        operation: { value: operation, type: "string" },
        payload: { value: JSON.stringify(body ?? {}), type: "string" },
      },
    },
  };
  const response = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok && response.status !== 202) {
    throw new ApiError(response.status, "UPSTREAM_ERROR", `Starting the job failed (HTTP ${response.status}).`);
  }
}

const POLL_FAST_MS = 2000;
const POLL_SLOW_MS = 5000;
const POLL_SLOW_AFTER_MS = 30000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Reserves a job (`POST /runs/{id}/jobs`), starts `nexora_stage_runner`,
 * polls `GET /runs/{id}` until `active_job` settles, then returns the job's
 * result body (what the old synchronous endpoint used to return directly). */
export async function runJob<T>(runId: string, operation: string, body: unknown = {}): Promise<T> {
  const reservation = await call<{ job_id: string; run: RunDetail }>(
    "POST",
    `/runs/${encodeURIComponent(runId)}/jobs`,
    {},
    { operation, body },
  );
  const jobId = reservation.job_id;
  await startStageRunnerJob(jobId, runId, operation, body);

  const started = Date.now();
  for (;;) {
    await sleep(Date.now() - started > POLL_SLOW_AFTER_MS ? POLL_SLOW_MS : POLL_FAST_MS);
    const run = await call<RunDetail>("GET", `/runs/${encodeURIComponent(runId)}`);
    const activeJob = run.active_job;
    if (!activeJob || activeJob.job_id !== jobId || !["queued", "running"].includes(activeJob.status)) {
      break;
    }
  }

  const result = await call<DispatchEnvelope>(
    "GET",
    `/runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(jobId)}`,
  );
  raiseIfError(result);
  return result.body as T;
}

export const api = {
  listRuns: () => call<RunList>("GET", "/runs", { limit: 200 }),
  getRun: (id: string) => call<RunDetail>("GET", `/runs/${encodeURIComponent(id)}`),
  createRun: (body: CreateRunBody) => call<RunDetail>("POST", "/runs", {}, body),
  setScope: (id: string, body: ScopeBody) => runJob<RunDetail>(id, "PUT /runs/{id}/scope", body),
  compile: (id: string) => runJob<CompileResult>(id, "POST /runs/{id}/compile", {}),
  answerQuestion: (id: string, qid: string, answer: string) =>
    runJob<QuestionResult>(id, `POST /runs/{id}/questions/${encodeURIComponent(qid)}/answer`, { answer }),
  dismissQuestion: (id: string, qid: string, reason: string) =>
    call<QuestionResult>("POST", `/runs/${encodeURIComponent(id)}/questions/${encodeURIComponent(qid)}/dismiss`, {}, {
      reason,
    }),
  checkG1: (id: string) => call<GateResult>("POST", `/runs/${encodeURIComponent(id)}/gates/G1/check`),
  checkG2: (id: string) => call<GateResult>("POST", `/runs/${encodeURIComponent(id)}/gates/G2/check`),
  listApprovals: (id: string) => call<ApprovalRecord[]>("GET", `/runs/${encodeURIComponent(id)}/approvals`),
  // Metadata harvester, profiler and relationship analysis (T-301 - T-303):
  // harvest, profile and analyze are jobs; reading the inventory or the
  // relationship evidence, and everything else on the Inventory tab (G2,
  // exceptions, data gaps), is a udf (CONTRACTS.md#fabric-operations).
  getInventory: (id: string) => call<MetadataInventory>("GET", `/runs/${encodeURIComponent(id)}/inventory`),
  getRelationships: (id: string) =>
    call<RelationshipEvidence[]>("GET", `/runs/${encodeURIComponent(id)}/relationships`),
  harvest: (id: string, schemas?: string[]) =>
    runJob<HarvestResult>(id, "POST /runs/{id}/harvest", schemas && schemas.length > 0 ? { schemas } : {}),
  profile: (id: string) => runJob<StageCounts>(id, "POST /runs/{id}/profile", {}),
  analyzeRelationships: (id: string) =>
    runJob<StageCounts>(id, "POST /runs/{id}/relationships/analyze", {}),
  approveException: (id: string, table: string, rationale: string) =>
    call<ExceptionResult>("POST", `/runs/${encodeURIComponent(id)}/exceptions`, {}, { table, rationale }),
  raiseDataGap: (id: string, description: string) =>
    call<RunDetail>("POST", `/runs/${encodeURIComponent(id)}/data-gaps`, {}, { description }),
  // Planning (T-401 - T-405): resolving sources, checking feasibility and
  // proposing bridges are jobs (they call the model and the SQL endpoint);
  // generating/reading/editing the plan and Gate G3 are deterministic udf
  // calls over control files only (CONTRACTS.md#fabric-operations).
  getSourceResolution: (id: string) =>
    call<SourceResolution>("GET", `/runs/${encodeURIComponent(id)}/plan/sources`),
  resolveSources: (id: string) => runJob<SourceResolution>(id, "POST /runs/{id}/plan/sources", {}),
  checkFeasibility: (id: string) =>
    runJob<{ kpis: { kpi: string; status: string }[]; run: RunDetail }>(id, "POST /runs/{id}/plan/feasibility", {}),
  checkG3: (id: string) => call<GateResult>("POST", `/runs/${encodeURIComponent(id)}/gates/G3/check`),
  getPlan: (id: string) => call<PlanResult>("GET", `/runs/${encodeURIComponent(id)}/plan`),
  generatePlan: (id: string) => call<PlanResult>("POST", `/runs/${encodeURIComponent(id)}/plan`),
  editPlan: (id: string, expectedHash: string, ops: PlanOp[]) =>
    call<PlanEditResult>("PATCH", `/runs/${encodeURIComponent(id)}/plan`, {}, { expected_hash: expectedHash, ops }),
  /** `REGENERATE` recompiles (a job); every other action (ACCEPT, EDIT, REJECT,
   * EXPLAIN) runs as a udf (CONTRACTS.md#fabric-operations: `_approve_host`). */
  approve: (id: string, body: ApprovalBody) =>
    body.action === "REGENERATE"
      ? runJob<ApprovalResult>(id, "POST /runs/{id}/approvals", body)
      : call<ApprovalResult>("POST", `/runs/${encodeURIComponent(id)}/approvals`, {}, body),
  getMe: () => me(),
  // Semantic model (T-501 - T-507): generating the TMDL is a deterministic udf
  // call over control files only; measures, validate, repair and bridge
  // generation call the model or a Fabric/SQL endpoint, so they are jobs
  // (CONTRACTS.md#fabric-operations).
  generateModel: (id: string) => call<ModelGenerateResult>("POST", `/runs/${encodeURIComponent(id)}/model/generate`),
  addMeasures: (id: string) => runJob<MeasuresResult>(id, "POST /runs/{id}/model/measures", {}),
  validateModel: (id: string) => runJob<ModelValidateResult>(id, "POST /runs/{id}/model/validate", {}),
  repairModel: (id: string) => runJob<RepairResult>(id, "POST /runs/{id}/model/repair", {}),
  generateBridge: (id: string, bridgeId: string) =>
    runJob<BridgeGenerateResult>(id, `POST /runs/{id}/model/bridges/${encodeURIComponent(bridgeId)}/generate`, {}),
  getModelReview: (id: string) => call<ModelReview>("GET", `/runs/${encodeURIComponent(id)}/model/review`),
  // Report (T-601 - T-604): planning, generating, validating and repairing
  // the report call the model or a Fabric/Power BI endpoint, so they are jobs;
  // reading the plan and the review are udf calls over control files only
  // (CONTRACTS.md#fabric-operations). `approve` (above) already sends a
  // REGENERATE on the Report artifact as a job, every other action as a udf.
  getReportPlan: (id: string) => call<ReportPlanResult>("GET", `/runs/${encodeURIComponent(id)}/report/plan`),
  generateReportPlan: (id: string) => runJob<ReportPlanResult>(id, "POST /runs/{id}/report/plan", {}),
  generateReport: (id: string) => runJob<ReportGenerateResult>(id, "POST /runs/{id}/report/generate", {}),
  validateReport: (id: string) => runJob<ReportValidateResult>(id, "POST /runs/{id}/report/validate", {}),
  repairReport: (id: string) => runJob<RepairResult>(id, "POST /runs/{id}/report/repair", {}),
  getReportReview: (id: string) => call<ReportReview>("GET", `/runs/${encodeURIComponent(id)}/report/review`),
  /** `path` is the API path from `image_url` (`/runs/{id}/files/...`), fetched as the UDF's
   * base64 body (`get_run_file`): an `<img>` can't send the function's bearer token. */
  getRunFile: (path: string) => call<RunFile>("GET", path),
  // Audit trail and traceability (T-703): both are udf calls over control files only
  // (CONTRACTS.md#fabric-operations).
  getAudit: (id: string, offset: number, limit: number) =>
    call<AuditPage>("GET", `/runs/${encodeURIComponent(id)}/audit`, { offset, limit }),
  getTrace: (id: string, visualId: string) =>
    call<TraceResult>("GET", `/runs/${encodeURIComponent(id)}/trace`, { visual_id: visualId }),
};
