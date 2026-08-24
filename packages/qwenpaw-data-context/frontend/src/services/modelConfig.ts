import { request } from "./request";

const BASE = "/api/system/model-config";
const ENTRY_CHECK_STORAGE_KEY = "qwenpaw-data:model-config-entry-check:v1";

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

export interface ModelConfigResponse {
  llm: LLMConfig;
  embedding: EmbeddingConfig;
}

export function hasConfiguredModelApiKey(
  config: Partial<ModelConfigResponse>,
): boolean {
  return Boolean(
    textValue(config.llm?.api_key) || textValue(config.embedding?.api_key),
  );
}

function textValue(value: string | null | undefined): string {
  return value?.trim() ?? "";
}

function isLocalEndpoint(baseUrl: string): boolean {
  try {
    const hostname = new URL(baseUrl).hostname;
    return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
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
  const embeddingBaseUrl = textValue(config.embedding?.base_url) || llmBaseUrl;
  const embeddingApiKey = textValue(config.embedding?.api_key) || llmApiKey;
  const embeddingDim = config.embedding?.dim;

  const llmComplete = Boolean(
    llmBaseUrl &&
      llmModel &&
      hasUsableCredentials(llmBaseUrl, llmApiKey),
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

export interface TestResult {
  success: boolean;
  message: string;
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
  rebuild_required: boolean;
  job_id?: string | null;
}

export type RebuildJobStatus = "pending" | "running" | "success" | "failed";

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

function jsonOptions(method: "POST" | "PUT", body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

export const modelConfigApi = {
  get: () => request<ModelConfigResponse>(`${BASE}/`),
  updateLLM: (payload: LLMConfigPayload) =>
    request<{ llm: LLMConfig }>(`${BASE}/llm`, jsonOptions("PUT", payload)),
  testLLM: (payload: LLMConfigPayload) =>
    request<TestResult>(`${BASE}/llm/test`, jsonOptions("POST", payload)),
  updateEmbedding: (payload: EmbeddingConfigPayload) =>
    request<EmbeddingUpdateResponse>(
      `${BASE}/embedding`,
      jsonOptions("PUT", payload),
    ),
  testEmbedding: (payload: EmbeddingConfigPayload) =>
    request<TestResult>(
      `${BASE}/embedding/test`,
      jsonOptions("POST", payload),
    ),
  getLatestRebuildJob: () =>
    request<RebuildJob | null>(`${BASE}/embedding/jobs/latest`),
  getRebuildJob: (jobId: string) =>
    request<RebuildJob>(
      `${BASE}/embedding/jobs/${encodeURIComponent(jobId)}`,
    ),
  retryRebuildJob: (jobId: string) =>
    request<{ status: "retrying"; job_id: string }>(
      `${BASE}/embedding/jobs/${encodeURIComponent(jobId)}/retry`,
      { method: "POST" },
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
    modelConfigApi.testLLM({ ...config.llm, api_key: "" }),
    modelConfigApi.testEmbedding({ ...config.embedding, api_key: "" }),
  ]);
  const { llmComplete, embeddingComplete } = getModelConfigCompleteness(config);

  return {
    llm:
      llmResult.status === "fulfilled"
        ? llmResult.value
        : rejectedTestResult(llmResult.reason),
    embedding:
      embeddingResult.status === "fulfilled"
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
