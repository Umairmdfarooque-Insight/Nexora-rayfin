// The Fabric workload host's client API (the Workload Development Kit /
// Extensibility Toolkit's `WorkloadClientAPI`). The real type comes from the
// Starter-Kit's own package once this project is dropped into it (see
// README.md); this is the minimal surface Rayfin needs, so the rest of the
// code doesn't depend on the exact SDK import path until then.
export interface FabricHostClient {
  /** A bearer token for `resource` (a resource URI such as
   * "https://api.fabric.microsoft.com" or "https://analysis.windows.net/powerbi/api"),
   * acquired through the host so Rayfin never handles its own sign-in
   * (T-105: "Rayfin uses the Fabric host's sign-in"). */
  acquireAccessToken(resource: string): Promise<string>;
  navigation: {
    navigate(path: string): void;
  };
  notifications: {
    show(message: string, intent: "success" | "error" | "info" | "warning"): void;
  };
}

let current: FabricHostClient | null = null;

export function setFabricHostClient(client: FabricHostClient | null): void {
  current = client;
}

export function getFabricHostClient(): FabricHostClient | null {
  return current;
}
