/** Defaults for a first-time embedding configuration. */
export const DEFAULT_EMBEDDING_MODEL = 'text-embedding-v3';
export const DEFAULT_EMBEDDING_DIM = 1024;

const EMBEDDING_MODEL_IDS: readonly string[] = [DEFAULT_EMBEDDING_MODEL, 'text-embedding-v4'];

/** Keep previously saved model IDs selectable even when absent from the catalog. */
export function embeddingModelOptions(savedModel?: string | null): { value: string; label: string }[] {
  const ids = savedModel && !EMBEDDING_MODEL_IDS.includes(savedModel)
    ? [savedModel, ...EMBEDDING_MODEL_IDS]
    : EMBEDDING_MODEL_IDS;
  return ids.map(id => ({ value: id, label: id }));
}
