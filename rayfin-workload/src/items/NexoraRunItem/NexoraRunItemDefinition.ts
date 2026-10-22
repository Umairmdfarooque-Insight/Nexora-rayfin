// The "Nexora Run" item's own stored payload (T-104 FR-5): "The item stores
// only the run_id. The run itself lives in the control store." Fabric persists
// this definition for the item; Rayfin never duplicates run data into it.
export interface NexoraRunItemDefinition {
  runId: string | null;
}

export const EMPTY_DEFINITION: NexoraRunItemDefinition = { runId: null };
