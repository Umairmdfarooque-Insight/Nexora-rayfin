// Thin client for the Nexora API. It only fetches and reports errors:
// all decisions (states, next actions, validation) come from the API.

import type {
  AuditPage,
  TraceResult,
  ApprovalBody,
  ApprovalRecord,
  ApprovalResult,
  CompileResult,
  CreateRunBody,
  ExceptionResult,
  GateResult,
  HarvestResult,
  Lakehouse,
  Me,
  ModelReview,
  ReportReview,
  MetadataInventory,
  PlanEditResult,
  PlanOp,
  PlanResult,
  QuestionResult,
  StageCounts,
  ValidationRun,
  RunDetail,
  RunList,
  ScopeBody,
  SourceResolution,
  Workspace,
} from "./types.ts";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"
).replace(/\/+$/, "");

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

/**
 * Returns an access token for the API, or null when sign-in isn't in use.
 * `forceRefresh` asks for a fresh token instead of a cached one.
 */
export type TokenProvider = (forceRefresh: boolean) => Promise<string | null>;

let tokenProvider: TokenProvider | null = null;

export function setTokenProvider(provider: TokenProvider | null): void {
  tokenProvider = provider;
}

async function fetchOnce(
  path: string,
  init: RequestInit | undefined,
  forceRefresh: boolean,
): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (tokenProvider) {
    let token: string | null;
    try {
      token = await tokenProvider(forceRefresh);
    } catch {
      throw new ApiError(
        401,
        "UNAUTHENTICATED",
        "Your session has expired. Reload the page to sign in again.",
      );
    }
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  try {
    return await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(
      0,
      "NETWORK",
      `Can't reach the Nexora API at ${API_BASE_URL}. Check that it is running.`,
    );
  }
}

async function readError(response: Response): Promise<ApiError> {
  let body: { error?: { code?: string; message?: string; details?: unknown } } | null =
    null;
  try {
    body = await response.json();
  } catch {
    // Not JSON: fall through to the generic message.
  }
  const error = body?.error;
  return new ApiError(
    response.status,
    error?.code ?? "UNKNOWN",
    error?.message ?? `The API returned HTTP ${response.status}.`,
    fieldMessages(error?.details),
  );
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response = await fetchOnce(path, init, false);
  let error = response.ok ? null : await readError(response);

  // An expired token is refreshed silently and the call is retried once.
  if (error?.code === "TOKEN_EXPIRED" && tokenProvider) {
    response = await fetchOnce(path, init, true);
    error = response.ok ? null : await readError(response);
  }

  if (error) {
    throw error;
  }
  return (await response.json()) as T;
}

/** A file of the run (a page render), fetched with the sign-in token, as a Blob to show. */
export async function fetchRunFile(path: string): Promise<Blob> {
  let response = await fetchOnce(path, undefined, false);
  let error = response.ok ? null : await readError(response);
  if (error?.code === "TOKEN_EXPIRED" && tokenProvider) {
    response = await fetchOnce(path, undefined, true);
    error = response.ok ? null : await readError(response);
  }
  if (error) {
    throw error;
  }
  return await response.blob();
}

export const api = {
  listRuns: () => apiFetch<RunList>("/runs?limit=200"),
  getRun: (id: string) => apiFetch<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  createRun: (body: CreateRunBody) =>
    apiFetch<RunDetail>("/runs", { method: "POST", body: JSON.stringify(body) }),
  setScope: (id: string, body: ScopeBody) =>
    apiFetch<RunDetail>(`/runs/${encodeURIComponent(id)}/scope`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getMe: () => apiFetch<Me>("/me"),
  compile: (id: string) =>
    apiFetch<CompileResult>(`/runs/${encodeURIComponent(id)}/compile`, { method: "POST" }),
  answerQuestion: (id: string, qid: string, answer: string) =>
    apiFetch<QuestionResult>(
      `/runs/${encodeURIComponent(id)}/questions/${encodeURIComponent(qid)}/answer`,
      { method: "POST", body: JSON.stringify({ answer }) },
    ),
  dismissQuestion: (id: string, qid: string, reason: string) =>
    apiFetch<QuestionResult>(
      `/runs/${encodeURIComponent(id)}/questions/${encodeURIComponent(qid)}/dismiss`,
      { method: "POST", body: JSON.stringify({ reason }) },
    ),
  checkG1: (id: string) =>
    apiFetch<GateResult>(`/runs/${encodeURIComponent(id)}/gates/G1/check`, {
      method: "POST",
    }),
  checkG2: (id: string) =>
    apiFetch<GateResult>(`/runs/${encodeURIComponent(id)}/gates/G2/check`, {
      method: "POST",
    }),
  getInventory: (id: string) =>
    apiFetch<MetadataInventory>(`/runs/${encodeURIComponent(id)}/inventory`),
  harvest: (id: string, schemas?: string[]) =>
    apiFetch<HarvestResult>(`/runs/${encodeURIComponent(id)}/harvest`, {
      method: "POST",
      body: JSON.stringify(schemas && schemas.length > 0 ? { schemas } : {}),
    }),
  profile: (id: string) =>
    apiFetch<StageCounts>(`/runs/${encodeURIComponent(id)}/profile`, { method: "POST" }),
  analyzeRelationships: (id: string) =>
    apiFetch<StageCounts>(`/runs/${encodeURIComponent(id)}/relationships/analyze`, {
      method: "POST",
    }),
  approveException: (id: string, table: string, rationale: string) =>
    apiFetch<ExceptionResult>(`/runs/${encodeURIComponent(id)}/exceptions`, {
      method: "POST",
      body: JSON.stringify({ table, rationale }),
    }),
  raiseDataGap: (id: string, description: string) =>
    apiFetch<RunDetail>(`/runs/${encodeURIComponent(id)}/data-gaps`, {
      method: "POST",
      body: JSON.stringify({ description }),
    }),
  approve: (id: string, body: ApprovalBody) =>
    apiFetch<ApprovalResult>(`/runs/${encodeURIComponent(id)}/approvals`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listApprovals: (id: string) =>
    apiFetch<ApprovalRecord[]>(`/runs/${encodeURIComponent(id)}/approvals`),
  checkG3: (id: string) =>
    apiFetch<GateResult>(`/runs/${encodeURIComponent(id)}/gates/G3/check`, {
      method: "POST",
    }),
  getPlan: (id: string) => apiFetch<PlanResult>(`/runs/${encodeURIComponent(id)}/plan`),
  generatePlan: (id: string) =>
    apiFetch<PlanResult>(`/runs/${encodeURIComponent(id)}/plan`, { method: "POST" }),
  editPlan: (id: string, expectedHash: string, ops: PlanOp[]) =>
    apiFetch<PlanEditResult>(`/runs/${encodeURIComponent(id)}/plan`, {
      method: "PATCH",
      body: JSON.stringify({ expected_hash: expectedHash, ops }),
    }),
  getSourceResolution: (id: string) =>
    apiFetch<SourceResolution>(`/runs/${encodeURIComponent(id)}/plan/sources`),
  resolveSources: (id: string) =>
    apiFetch<SourceResolution>(`/runs/${encodeURIComponent(id)}/plan/sources`, { method: "POST" }),
  checkFeasibility: (id: string) =>
    apiFetch<{ kpis: { kpi: string; status: string }[]; run: RunDetail }>(
      `/runs/${encodeURIComponent(id)}/plan/feasibility`,
      { method: "POST" },
    ),
  getModelReview: (id: string) => apiFetch<ModelReview>(`/runs/${encodeURIComponent(id)}/model/review`),
  validateModel: (id: string) =>
    apiFetch<{ report: ValidationRun; run: RunDetail }>(`/runs/${encodeURIComponent(id)}/model/validate`, {
      method: "POST",
    }),
  getReportReview: (id: string) => apiFetch<ReportReview>(`/runs/${encodeURIComponent(id)}/report/review`),
  validateReport: (id: string) =>
    apiFetch<{ report: ValidationRun; run: RunDetail }>(`/runs/${encodeURIComponent(id)}/report/validate`, {
      method: "POST",
    }),
  getAudit: (id: string, offset = 0, limit = 200) =>
    apiFetch<AuditPage>(`/runs/${encodeURIComponent(id)}/audit?offset=${offset}&limit=${limit}`),
  getTrace: (id: string, visualId: string) =>
    apiFetch<TraceResult>(`/runs/${encodeURIComponent(id)}/trace?visual_id=${encodeURIComponent(visualId)}`),
  listWorkspaces: () => apiFetch<Workspace[]>("/fabric/workspaces"),
  listLakehouses: (workspaceId: string) =>
    apiFetch<Lakehouse[]>(
      `/fabric/workspaces/${encodeURIComponent(workspaceId)}/lakehouses`,
    ),
};
