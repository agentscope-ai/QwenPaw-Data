import type { EmbeddingConfigPayload, LLMConfigPayload } from '../../api/modelConfigTypes.js';
import type { ModelProvider } from '../../api/types.js';

interface SessionModelDraft {
  default_provider_id: string;
  default_model_id: string;
  light_selection?: string;
}

export interface ActiveModelDraft extends SessionModelDraft {
  cm_provider_id?: string;
  cm_model_id?: string;
  embedding_provider_id?: string;
  embedding_model_id?: string;
  embedding_dim?: number;
}

/** A missing capability (e.g. internal or older deployment) keeps the integrated contract. */
export function hasSemanticModelSettings(contextConsole?: boolean) {
  return contextConsole !== false;
}

type SaveMode = 'host-only' | 'integrated' | 'blocked';

export function activeModelSaveMode(
  semanticEnabled: boolean,
  bridge: { loading: boolean; error: string | null; testing: string | null; config: unknown },
): SaveMode {
  if (!semanticEnabled) return 'host-only';
  return bridge.loading || bridge.error !== null || bridge.testing || !bridge.config
    ? 'blocked'
    : 'integrated';
}

/** Host preferences never include, validate, or write the semantic service's fields. */
export function sessionModelPayload(draft: SessionModelDraft) {
  const light = draft.light_selection ?? '';
  const separator = light.indexOf('/');
  return {
    default_provider_id: draft.default_provider_id,
    default_model_id: draft.default_model_id,
    light_provider_id: separator > 0 ? light.slice(0, separator) : null,
    light_model_id: separator > 0 ? light.slice(separator + 1) : null,
  };
}

export async function saveActiveModelDraft(
  mode: SaveMode,
  draft: ActiveModelDraft,
  providers: ModelProvider[],
  api: {
    updateActiveModels: (body: ReturnType<typeof sessionModelPayload>) => Promise<unknown>;
    updateLLM: (body: LLMConfigPayload) => Promise<unknown>;
    updateEmbedding: (body: EmbeddingConfigPayload) => Promise<unknown>;
  },
) {
  if (mode === 'blocked') throw new Error('DataBridge model settings are unavailable');
  const writes: [string, () => Promise<unknown>][] = [
    ['session', () => api.updateActiveModels(sessionModelPayload(draft))],
  ];
  if (mode === 'integrated') {
    const endpoint = (id?: string) => {
      const provider = providers.find((item) => item.id === id && item.configured);
      if (!provider) throw new Error('Select a configured Provider');
      return provider.base_url || provider.catalog_base_url;
    };
    // Prepare both semantic payloads before issuing any writes.
    const llm = {
      provider_id: draft.cm_provider_id,
      model: draft.cm_model_id,
      base_url: endpoint(draft.cm_provider_id),
    };
    const embedding = {
      provider_id: draft.embedding_provider_id,
      model: draft.embedding_model_id,
      dim: draft.embedding_dim,
      base_url: endpoint(draft.embedding_provider_id),
    };
    writes.push(
      ['DataBridge', () => api.updateLLM(llm)],
      ['Embedding', () => api.updateEmbedding(embedding)],
    );
  }
  const results = await Promise.allSettled(writes.map(async ([, write]) => write()));
  return results.flatMap((result, i) =>
    result.status === 'rejected'
      ? [{ section: writes[i][0], reason: result.reason as unknown }]
      : [],
  );
}
