// Workload configuration (T-104 API contract): "the home workspace id, the
// nexora_api item id and the nexora_stage_runner item id" live in the
// workload's own configuration, not in source. The Starter-Kit injects
// per-environment values from Workload/.env.dev|.env.test|.env.prod (see
// README.md) as build-time environment variables; this just reads them with
// safe empty-string fallbacks so the module can be imported before that
// wiring exists (local preview, unit tests).

export interface WorkloadConfig {
  homeWorkspaceId: string;
  stageRunnerItemId: string;
  /** The `call` function's public URL (T-102); Fabric generates one per
   * published function (see fabric-hosting.md's platform-limits table). */
  nexoraApiCallFunctionUrl: string;
  nexoraApiMeFunctionUrl: string;
}

declare const process: { env?: Record<string, string | undefined> } | undefined;

function setting(name: string, fallback = ""): string {
  try {
    return (typeof process !== "undefined" && process.env?.[name]) || fallback;
  } catch {
    return fallback;
  }
}

export function loadConfig(): WorkloadConfig {
  return {
    homeWorkspaceId: setting("NEXORA_HOME_WORKSPACE_ID"),
    stageRunnerItemId: setting("NEXORA_STAGE_RUNNER_ITEM_ID"),
    nexoraApiCallFunctionUrl: setting("NEXORA_API_CALL_FUNCTION_URL"),
    nexoraApiMeFunctionUrl: setting("NEXORA_API_ME_FUNCTION_URL"),
  };
}
