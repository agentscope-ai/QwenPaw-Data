import { requestAtUrl } from '../../../services/request.js';
import type {
  EmbeddingConfigPayload,
  EmbeddingUpdateResponse,
  LLMConfigPayload,
  LLMUpdateResponse,
  ModelConfigResponse,
  RebuildJob,
  RebuildRetried,
  RebuildStarted,
  TestResult,
} from './modelConfigTypes.js';

// Context is served at the origin root, independently of Host's /console API base.
const BASE = '/api/system/model-config';

function jsonOptions(method: 'POST' | 'PUT', body: unknown, signal?: AbortSignal): RequestInit {
  return { method, body: JSON.stringify(body), signal };
}

export const modelConfigApi = {
  get: (signal?: AbortSignal) => requestAtUrl<ModelConfigResponse>(`${BASE}/`, { signal }),
  updateLLM: (payload: LLMConfigPayload, signal?: AbortSignal) =>
    requestAtUrl<LLMUpdateResponse>(`${BASE}/llm`, jsonOptions('PUT', payload, signal)),
  updateEmbedding: (payload: EmbeddingConfigPayload, signal?: AbortSignal) =>
    requestAtUrl<EmbeddingUpdateResponse>(`${BASE}/embedding`, jsonOptions('PUT', payload, signal)),
  testLLM: (payload: LLMConfigPayload, signal?: AbortSignal) =>
    requestAtUrl<TestResult>(`${BASE}/llm/test`, jsonOptions('POST', payload, signal)),
  testEmbedding: (payload: EmbeddingConfigPayload, signal?: AbortSignal) =>
    requestAtUrl<TestResult>(`${BASE}/embedding/test`, jsonOptions('POST', payload, signal)),
  rebuildEmbeddings: (signal?: AbortSignal) =>
    requestAtUrl<RebuildStarted>(`${BASE}/embedding/rebuild`, { method: 'POST', signal }),
  getLatestRebuildJob: (signal?: AbortSignal) =>
    requestAtUrl<RebuildJob | null>(`${BASE}/embedding/jobs/latest`, { signal }),
  getRebuildJob: (jobId: string, signal?: AbortSignal) =>
    requestAtUrl<RebuildJob>(`${BASE}/embedding/jobs/${encodeURIComponent(jobId)}`, { signal }),
  retryRebuildJob: (jobId: string, signal?: AbortSignal) =>
    requestAtUrl<RebuildRetried>(`${BASE}/embedding/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: 'POST',
      signal,
    }),
};
