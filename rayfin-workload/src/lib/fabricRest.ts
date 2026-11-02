// Workspace and Lakehouse pickers (T-103 FR-1): read-only Fabric REST calls
// made directly with the workload host's own token for the signed-in user.
// Display only -- `PUT /runs/{id}/scope` is a job that verifies everything
// again before saving (T-103 constraint: "The pickers' result is never
// trusted. The job decides.").

import type { FabricHostClient } from "./fabricHost.ts";
import type { Lakehouse, Workspace } from "./types.ts";

const FABRIC_API = "https://api.fabric.microsoft.com/v1";
const FABRIC_RESOURCE = "https://api.fabric.microsoft.com";
const ONELAKE_TABLE_API = "https://onelake.table.fabric.microsoft.com/delta";
const STORAGE_RESOURCE = "https://storage.azure.com";
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

// Schema/table listing (New Run form, optional narrowing): OneLake's Unity-Catalog-
// compatible REST API, read the same way app/connectors/fabric_lakehouse.py does
// server-side, but called directly from the browser with the host's own storage token --
// display only, same as listWorkspaces/listLakehouses above (T-103's own rule: "the
// pickers' result is never trusted, the job decides" when PUT /runs/{id}/scope runs).
async function getAllOneLake(
  host: FabricHostClient,
  url: string,
  params: Record<string, string>,
  resultKey: string,
): Promise<Record<string, unknown>[]> {
  const token = await host.acquireAccessToken(STORAGE_RESOURCE);
  const items: Record<string, unknown>[] = [];
  let pageToken: string | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    const query = new URLSearchParams(pageToken ? { ...params, page_token: pageToken } : params);
    const response = await fetch(`${url}?${query.toString()}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) {
      throw new Error(`OneLake returned HTTP ${response.status}`);
    }
    const body = (await response.json()) as Record<string, unknown> & { next_page_token?: string };
    items.push(...((body[resultKey] as Record<string, unknown>[] | undefined) ?? []));
    if (!body.next_page_token) {
      return items;
    }
    pageToken = body.next_page_token;
  }
  throw new Error("OneLake returned too many pages");
}

export async function listSchemas(
  host: FabricHostClient,
  workspaceId: string,
  lakehouseId: string,
  lakehouseName: string,
): Promise<string[]> {
  const url = `${ONELAKE_TABLE_API}/${encodeURIComponent(workspaceId)}/${encodeURIComponent(lakehouseId)}/api/2.1/unity-catalog/schemas`;
  const items = await getAllOneLake(host, url, { catalog_name: `${lakehouseName}.Lakehouse` }, "schemas");
  return items.map((s) => String(s.name)).sort((a, b) => a.localeCompare(b));
}

export async function listTables(
  host: FabricHostClient,
  workspaceId: string,
  lakehouseId: string,
  lakehouseName: string,
  schema: string,
): Promise<string[]> {
  const url = `${ONELAKE_TABLE_API}/${encodeURIComponent(workspaceId)}/${encodeURIComponent(lakehouseId)}/api/2.1/unity-catalog/tables`;
  const items = await getAllOneLake(
    host,
    url,
    { catalog_name: `${lakehouseName}.Lakehouse`, schema_name: schema },
    "tables",
  );
  return items.map((t) => String(t.name)).sort((a, b) => a.localeCompare(b));
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
