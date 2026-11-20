import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { FormInstance } from 'antd';
import { modelConfigApi } from '@/features/console/api/modelConfig';
import type { ModelConfigResponse, TestResult } from '@/features/console/api/modelConfigTypes';
import type { ModelProvider } from '@/features/console/api/types';
import type { ActiveModelDraft } from '@/features/console/model/chat/activeModelSettings';
import { getHostAuthContext, subscribeHostAuth } from '@/services/auth';
import { DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL } from './embeddingModels';

export function useDataBridgeModelSettings(
  form: FormInstance<ActiveModelDraft>,
  providers: ModelProvider[],
  enabled: boolean,
) {
  const auth = useSyncExternalStore(subscribeHostAuth, getHostAuthContext);
  const [config, setConfig] = useState<ModelConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<'llm' | 'embedding' | null>(null);
  const [results, setResults] = useState<Partial<Record<'llm' | 'embedding', TestResult>>>({});
  const controller = useRef<AbortController | null>(null);
  const testingRef = useRef(false);
  const probe = useRef<AbortController | null>(null);

  const load = useCallback(
    async (fillForm = true) => {
      if (!enabled) return;
      controller.current?.abort();
      const current = new AbortController();
      controller.current = current;
      setLoading(true);
      setError(null);
      try {
        const value = await modelConfigApi.get(current.signal);
        if (current.signal.aborted) return;
        setConfig(value);
        if (fillForm)
          form.setFieldsValue({
            cm_provider_id: value.llm.provider_id ?? undefined,
            cm_model_id: value.llm.model ?? undefined,
            embedding_provider_id: value.embedding.provider_id ?? undefined,
            embedding_model_id: value.embedding.model || DEFAULT_EMBEDDING_MODEL,
            embedding_dim: value.embedding.dim ?? DEFAULT_EMBEDDING_DIM,
          });
      } catch (cause) {
        if (!current.signal.aborted)
          setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (!current.signal.aborted) setLoading(false);
      }
    },
    [form, enabled],
  );

  useEffect(() => {
    setResults({});
    setConfig(null);
    setTesting(null);
    testingRef.current = false;
    if (auth.ready) void load();
    return () => {
      controller.current?.abort();
      probe.current?.abort();
    };
  }, [auth.ready, auth.revision, load]);

  const test = async (channel: 'llm' | 'embedding') => {
    if (!enabled || testingRef.current || !auth.ready) return;
    testingRef.current = true;
    setTesting(channel);
    const revision = auth.revision;
    const current = new AbortController();
    probe.current = current;
    try {
      const fields: (keyof ActiveModelDraft)[] =
        channel === 'llm'
          ? ['cm_provider_id', 'cm_model_id']
          : ['embedding_provider_id', 'embedding_model_id', 'embedding_dim'];
      await form.validateFields(fields);
      const snapshot = form.getFieldsValue();
      // Build only the selected probe; the other channel may still be empty.
      const providerId =
        channel === 'llm' ? snapshot.cm_provider_id : snapshot.embedding_provider_id;
      const provider = providers.find((item) => item.id === providerId && item.configured);
      if (!provider) throw new Error('Select a configured Provider');
      const payload = {
        provider_id: providerId,
        base_url: provider.base_url || provider.catalog_base_url,
        model: channel === 'llm' ? snapshot.cm_model_id : snapshot.embedding_model_id,
        ...(channel === 'embedding' ? { dim: snapshot.embedding_dim } : {}),
      };
      const result = await (channel === 'llm'
        ? modelConfigApi.testLLM(payload, current.signal)
        : modelConfigApi.testEmbedding(payload, current.signal));
      if (current.signal.aborted || revision !== getHostAuthContext().revision) return;
      const latest = form.getFieldsValue();
      if (fields.some((field) => latest[field] !== snapshot[field])) return;
      setResults((old) => ({ ...old, [channel]: result }));
      if (channel === 'embedding' && result.success && result.detected_dim) {
        form.setFieldValue('embedding_dim', result.detected_dim);
      }
    } catch (cause) {
      if (
        !current.signal.aborted &&
        revision === getHostAuthContext().revision &&
        !(cause && typeof cause === 'object' && 'errorFields' in cause)
      ) {
        setResults((old) => ({
          ...old,
          [channel]: {
            success: false,
            message: cause instanceof Error ? cause.message : String(cause),
          },
        }));
      }
    } finally {
      if (!current.signal.aborted && revision === getHostAuthContext().revision) {
        testingRef.current = false;
        setTesting(null);
      }
    }
  };

  const clearResults = () => {
    probe.current?.abort();
    testingRef.current = false;
    setTesting(null);
    setResults({});
  };
  return { config, loading, error, testing, results, load, test, auth, clearResults };
}
