import { ApiRequestError } from './apiError';
import { request } from './request';

const BASE = '/api/system/model-config';
const ENTRY_CHECK_STORAGE_KEY = 'qwenpaw-data:model-config-entry-check:v1';

/** Backend error codes for per-user model configuration. */
export const MODEL_CONFIG_REQUIRED = 'model_config_required';
export const EMBEDDING_SPACE_CONFLICT = 'embedding_space_conflict';
export const REBUILD_ALREADY_ACTIVE = 'rebuild_already_active';
export const REBUILD_FORBIDDEN = 'rebuild_forbidden';
export const INVALID_MODEL_ENDPOINT = 'invalid_model_endpoint';

export interface LLMConfig {
  base_url?: string | null;
  model?: string | null;
  aux_model?: string | null;
  api_key?: string | null;
}

export interface EmbeddingConfig {
  model?: string;
  base_url?: string;
  api_key?: string;
  dim?: number;
}

/** The workspace's vector space, and whether my embedding config matches it. */
export interface EmbeddingSpace {
  adopted: boolean;
  /** null when the workspace has no fingerprint yet, or I have no config. */
  aligned: boolean | null;
  model?: string;
  base_url?: string;
  dim?: number | null;
  fingerprint?: string;
}

/** 密文无法解密；非敏感字段仍返回，需重新填写密钥。 */
export type ModelConfigKeyStatus = 'ok' | 'undecryptable';

export interface ModelConfigResponse {
  /** False until this user saves their own credentials. */
  configured: boolean;
  key_status: ModelConfigKeyStatus;
  /** 是否仍使用播种的共享凭据。 */
  seeded: boolean;
  llm: LLMConfig;
  embedding: EmbeddingConfig;
  embedding_space: EmbeddingSpace;
}

/** Both sides of an embedding-space mismatch, as returned with a 409. */
export interface EmbeddingSpaceConflictDetails {
  workspace: { model: string; base_url: string; dim: number | null; fingerprint: string };
  mine: { model: string; base_url: string; dim: number | null; fingerprint: string };
}

function errorCode(error: unknown): string | undefined {
  return error instanceof ApiRequestError ? error.code : undefined;
}

/** The caller has no model configuration yet (HTTP 412). */
export function isModelConfigRequired(error: unknown): boolean {
  return errorCode(error) === MODEL_CONFIG_REQUIRED;
}

/** My embedding config disagrees with the workspace vector space (HTTP 409). */
export function isEmbeddingSpaceConflict(error: unknown): boolean {
  return errorCode(error) === EMBEDDING_SPACE_CONFLICT;
}

/** The workspace is already rebuilding; the embedding model cannot change now (409). */
export function isRebuildAlreadyActive(error: unknown): boolean {
  return errorCode(error) === REBUILD_ALREADY_ACTIVE;
}

/** Only workspace admins may rewrite the workspace's vectors (403). */
export function isRebuildForbidden(error: unknown): boolean {
  return errorCode(error) === REBUILD_FORBIDDEN;
}

/** The submitted base_url failed the egress policy (400). */
export function isInvalidModelEndpoint(error: unknown): boolean {
  return errorCode(error) === INVALID_MODEL_ENDPOINT;
}

export function embeddingSpaceConflictDetails(
  error: unknown,
): EmbeddingSpaceConflictDetails | null {
  if (!isEmbeddingSpaceConflict(error)) return null;
  const details = (error as ApiRequestError).details;
  if (!details || typeof details !== 'object') return null;
  const { workspace, mine } = details as Record<string, unknown>;
  if (!workspace || !mine) return null;
  return { workspace, mine } as EmbeddingSpaceConflictDetails;
}

function textValue(value: string | null | undefined): string {
  return value?.trim() ?? '';
}

function isLocalEndpoint(baseUrl: string): boolean {
  try {
    const hostname = new URL(baseUrl).hostname;
    return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
  } catch {
    return false;
  }
}

function hasUsableCredentials(baseUrl: string, apiKey: string): boolean {
  return Boolean(apiKey) || isLocalEndpoint(baseUrl);
}

function getModelConfigCompleteness(config: Partial<ModelConfigResponse>) {
  const llmBaseUrl = textValue(config.llm?.base_url);
  const llmModel = textValue(config.llm?.model);
  const llmApiKey = textValue(config.llm?.api_key);
  const embeddingModel = textValue(config.embedding?.model);
  // Embedding 通道必须自带 base_url 和 api_key。
  const embeddingBaseUrl = textValue(config.embedding?.base_url);
  const embeddingApiKey = textValue(config.embedding?.api_key);
  const embeddingDim = config.embedding?.dim;

  const llmComplete = Boolean(
    llmBaseUrl && llmModel && hasUsableCredentials(llmBaseUrl, llmApiKey),
  );
  const embeddingComplete = Boolean(
    embeddingBaseUrl &&
    embeddingModel &&
    Number.isInteger(embeddingDim) &&
    Number(embeddingDim) > 0 &&
    hasUsableCredentials(embeddingBaseUrl, embeddingApiKey),
  );

  return { llmComplete, embeddingComplete };
}

export type LLMConfigPayload = Partial<LLMConfig>;
export type EmbeddingConfigPayload = Partial<EmbeddingConfig>;

/** 连通性测试的稳定失败分类。 */
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
  success: boolean;
  /** English fallback; prefer localizing `code`. */
  message: string;
  code?: TestResultCode | null;
  detected_dim?: number | null;
}

export interface ModelConfigEntryCheck {
  llm: TestResult;
  embedding: TestResult;
  llmComplete: boolean;
  embeddingComplete: boolean;
}

export interface EmbeddingUpdateResponse {
  embedding: EmbeddingConfig;
}

export type RebuildJobStatus = 'pending' | 'running' | 'success' | 'failed';

export interface RebuildProgress {
  phase: string;
  current_label: string;
  labels_done: number;
  labels_total: number;
}

export interface RebuildJob {
  job_id: string;
  status: RebuildJobStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  progress: RebuildProgress;
  config_snapshot: Record<string, unknown>;
}

function jsonOptions(method: 'POST' | 'PUT', body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

export const modelConfigApi = {
  get: () => request<ModelConfigResponse>(`${BASE}/`),
  clear: () => request<{ deleted: boolean }>(`${BASE}/`, { method: 'DELETE' }),
  updateLLM: (payload: LLMConfigPayload) =>
    request<{ llm: LLMConfig }>(`${BASE}/llm`, jsonOptions('PUT', payload)),
  updateEmbedding: (payload: EmbeddingConfigPayload) =>
    request<EmbeddingUpdateResponse>(`${BASE}/embedding`, jsonOptions('PUT', payload)),
  testLLM: (payload: LLMConfigPayload) =>
    request<TestResult>(`${BASE}/llm/test`, jsonOptions('POST', payload)),
  testEmbedding: (payload: EmbeddingConfigPayload) =>
    request<TestResult>(`${BASE}/embedding/test`, jsonOptions('POST', payload)),
  /** Rebuild every vector in the graph with the saved embedding config. */
  rebuildEmbeddings: () =>
    request<{ job_id: string }>(`${BASE}/embedding/rebuild`, { method: 'POST' }),
  getLatestRebuildJob: () => request<RebuildJob | null>(`${BASE}/embedding/jobs/latest`),
  getRebuildJob: (jobId: string) =>
    request<RebuildJob>(`${BASE}/embedding/jobs/${encodeURIComponent(jobId)}`),
  retryRebuildJob: (jobId: string) =>
    request<{ status: 'retrying'; job_id: string }>(
      `${BASE}/embedding/jobs/${encodeURIComponent(jobId)}/retry`,
      { method: 'POST' },
    ),
};

function rejectedTestResult(reason: unknown): TestResult {
  return {
    success: false,
    message: reason instanceof Error ? reason.message : String(reason),
  };
}

/** Test both model connections in parallel without sending masked API keys. */
export async function checkModelConfigConnections(
  config: ModelConfigResponse,
): Promise<ModelConfigEntryCheck> {
  const [llmResult, embeddingResult] = await Promise.allSettled([
    modelConfigApi.testLLM({ ...config.llm, api_key: '' }),
    modelConfigApi.testEmbedding({ ...config.embedding, api_key: '' }),
  ]);
  const { llmComplete, embeddingComplete } = getModelConfigCompleteness(config);

  return {
    llm: llmResult.status === 'fulfilled' ? llmResult.value : rejectedTestResult(llmResult.reason),
    embedding:
      embeddingResult.status === 'fulfilled'
        ? embeddingResult.value
        : rejectedTestResult(embeddingResult.reason),
    llmComplete,
    embeddingComplete,
  };
}

export function storeModelConfigEntryCheck(result: ModelConfigEntryCheck): void {
  try {
    window.sessionStorage.setItem(ENTRY_CHECK_STORAGE_KEY, JSON.stringify(result));
  } catch {
    // Redirect still works if sessionStorage is unavailable.
  }
}

export function readModelConfigEntryCheck(): ModelConfigEntryCheck | null {
  try {
    const serialized = window.sessionStorage.getItem(ENTRY_CHECK_STORAGE_KEY);
    return serialized ? (JSON.parse(serialized) as ModelConfigEntryCheck) : null;
  } catch {
    return null;
  }
}

export function clearModelConfigEntryCheck(): void {
  try {
    window.sessionStorage.removeItem(ENTRY_CHECK_STORAGE_KEY);
  } catch {
    // The in-memory result still renders if sessionStorage is unavailable.
  }
}
