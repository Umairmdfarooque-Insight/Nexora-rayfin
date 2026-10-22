// Workspace and Lakehouse pickers (T-103 FR-1): read-only Fabric REST calls
// made directly with the workload host's own token for the signed-in user.
// Display only -- `PUT /runs/{id}/scope` is a job that verifies everything
// again before saving (T-103 constraint: "The pickers' result is never
// trusted. The job decides.").

import type { FabricHostClient } from "./fabricHost.ts";
import type { Lakehouse, Workspace } from "./types.ts";

const FABRIC_API = "https://api.fabric.microsoft.com/v1";
const FABRIC_RESOURCE = "https://api.fabric.microsoft.com";
const MAX_PAGES = 1000;

async function getAll(host: FabricHostClient, url: string): Promise<Record<string, unknown>[]> {
  const token = await host.acquireAccessToken(FABRIC_RESOURCE);
  const items: Record<string, unknown>[] = [];
  let continuationToken: string | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    const withToken = continuationToken
      ? `${url}${url.includes("?") ? "&" : "?"}continuationToken=${encodeURIComponent(continuationToken)}`
      : url;
    const response = await fetch(withToken, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) {
      throw new Error(`Fabric returned HTTP ${response.status}`);
    }
    const body = (await response.json()) as {
      value?: Record<string, unknown>[];
      continuationToken?: string;
    };
    items.push(...(body.value ?? []));
    if (!body.continuationToken) {
      return items;
    }
    continuationToken = body.continuationToken;
  }
  throw new Error("Fabric returned too many pages");
}

export async function listWorkspaces(host: FabricHostClient): Promise<Workspace[]> {
  const items = await getAll(host, `${FABRIC_API}/workspaces`);
  return items.map((w) => ({
    id: String(w.id),
    display_name: String(w.displayName ?? ""),
    type: String(w.type ?? ""),
  }));
}

export async function listLakehouses(host: FabricHostClient, workspaceId: string): Promise<Lakehouse[]> {
  const items = await getAll(host, `${FABRIC_API}/workspaces/${encodeURIComponent(workspaceId)}/lakehouses`);
  return items.map((item) => {
    const properties = (item.properties as Record<string, unknown> | undefined) ?? {};
    const endpoint = (properties.sqlEndpointProperties as Record<string, unknown> | undefined) ?? {};
    const server = endpoint.connectionString as string | undefined;
    const name = String(item.displayName ?? "");
    return {
      id: String(item.id),
      display_name: name,
      workspace_id: String(item.workspaceId ?? workspaceId),
      sql_endpoint: server ? { server, database: name } : null,
    };
  });
}
