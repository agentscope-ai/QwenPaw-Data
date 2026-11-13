export interface EmbeddedIdentity {
  user_id: string; // Authenticated user.
  tenant_id: string | null; // Null for platform identity.
  active_workspace_id: string; // Selected workspace.
  role: string; // Authoritative role.
  capabilities: string[]; // Explicit server authorization.
  ready: boolean; // Authentication and permission data resolved.
  revision: number; // Changes invalidate pending page operations.
}
export interface EmbeddedModuleContext {
  signal: AbortSignal; // Aborts on session/identity change or timeout.
  assertCurrent: () => void; // Re-check after every asynchronous step.
}
export interface LocatedDataset {
  record: import('../types/datasetManagement').DatasetResponse; // Normalized existing page record.
  dataset_id: number; // Positive stable dataset identifier.
  domain_id: number; // Positive business domain identifier.
  datasource_id: string; // Authorized source identifier.
  dataset_name: string; // Display name.
  description: string; // Null normalized to an empty draft.
}
