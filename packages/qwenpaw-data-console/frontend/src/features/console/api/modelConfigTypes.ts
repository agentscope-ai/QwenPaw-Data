/** Existing Context model-config API contract. Keys in responses are masked. */
export interface LLMConfig {
  provider_id?: string | null; // Reference to the current user's Host Provider; no copied Key.
  base_url?: string | null; // LLM service endpoint.
  model?: string | null; // Independently selected DataBridge model.
  aux_model?: string | null; // Legacy auxiliary model returned by Context.
  api_key?: string | null; // Masked stored credential; never resubmit it.
}

export interface EmbeddingConfig {
  provider_id?: string | null; // Reference to the current user's Host Provider; no copied Key.
  base_url?: string; // Embedding service endpoint.
  model?: string; // Embedding model identifier.
  api_key?: string; // Masked stored credential; never resubmit it.
  dim?: number; // Positive integer vector dimension.
}

export interface EmbeddingSpace {
  adopted: boolean; // Whether this workspace has an established vector space.
  aligned: boolean | null; // Whether the user's configuration matches that space.
  model?: string; // Workspace embedding model identifier.
  base_url?: string; // Workspace embedding endpoint.
  dim?: number | null; // Workspace vector dimension.
  fingerprint?: string; // Server-computed vector-space identity.
}

export interface ModelConfigResponse {
  missing_fields?: string[]; // Server-reported incomplete fields for the configuration gate.
  configured: boolean; // Server's current CM configuration completeness verdict.
  key_status: 'ok' | 'undecryptable'; // Decryption status of CM credentials.
  seeded: boolean; // Whether the current configuration originated from migration.
  llm: LLMConfig; // DataBridge LLM configuration.
  embedding: EmbeddingConfig; // DataBridge embedding configuration.
  embedding_space: EmbeddingSpace; // Current workspace vector-space status.
}

export interface LLMConfigPayload {
  provider_id?: string | null; // Reference to the current user's Host Provider; no copied Key.
  base_url?: string | null; // Endpoint submitted for saving or testing.
  model?: string | null; // Selected DataBridge model.
  api_key?: string | null; // Legacy CM credential input; omitted to retain its value.
}

export interface EmbeddingConfigPayload {
  provider_id?: string | null; // Reference to the current user's Host Provider; no copied Key.
  base_url?: string; // Endpoint submitted for saving or testing.
  model?: string; // Selected embedding model.
  api_key?: string; // Legacy CM credential input; omitted to retain its value.
  dim?: number; // Positive integer vector dimension.
}

export type TestResultCode =
  | 'ok'
  | 'incomplete'
  | 'invalid_endpoint'
  | 'auth_failed'
  | 'model_not_found'
  | 'rate_limited'
  | 'endpoint_unreachable'
  | 'timeout'
  | 'bad_response';

export interface TestResult {
  success: boolean; // Whether the real model probe succeeded.
  message: string; // Service-provided result description.
  code?: TestResultCode | null; // Stable result category for display.
  detected_dim?: number | null; // Actual vector dimension measured by the probe.
}

export type RebuildJobStatus = 'pending' | 'running' | 'success' | 'failed';

export interface RebuildProgress {
  phase: string; // Current server task phase.
  current_label: string; // Graph label currently being processed.
  labels_done: number; // Number of completed labels.
  labels_total: number; // Total number of labels.
}

export interface RebuildJob {
  job_id: string; // Server-generated job identifier.
  status: RebuildJobStatus; // Current task lifecycle state.
  created_at: string; // Creation timestamp.
  started_at?: string | null; // Execution start timestamp.
  finished_at?: string | null; // Terminal timestamp.
  error?: string | null; // Failure reason returned by the server.
  progress: RebuildProgress; // Actual server-reported progress.
  config_snapshot: Record<string, unknown>; // Saved configuration used by the job.
}

export interface RebuildStarted {
  job_id: string; // Identifier to use for subsequent task queries.
}

export interface RebuildRetried extends RebuildStarted {
  status: 'retrying'; // Confirmation that the retry was accepted.
}

export interface LLMUpdateResponse {
  llm: LLMConfig; // Saved LLM configuration with masked credentials.
}

export interface EmbeddingUpdateResponse {
  embedding: EmbeddingConfig; // Saved embedding configuration with masked credentials.
}
