import type { SemanticConfigErrorBody, SemanticConfigPage } from '@/types/semanticConfig';

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return value != null && typeof value === 'object' && !Array.isArray(value);
}

export function isSemanticConfigErrorBody(value: unknown): value is SemanticConfigErrorBody {
  if (!isRecord(value)) return false;
  return (
    typeof value.timestamp === 'string' &&
    typeof value.status === 'number' &&
    typeof value.error === 'string' &&
    typeof value.message === 'string'
  );
}

export function assertSemanticConfigPage<T>(value: unknown): SemanticConfigPage<T> {
  if (
    !isRecord(value) ||
    !Array.isArray(value.records) ||
    typeof value.total !== 'number' ||
    typeof value.page !== 'number' ||
    typeof value.size !== 'number'
  ) {
    throw new TypeError('Invalid semantic-config pagination response');
  }

  return value as unknown as SemanticConfigPage<T>;
}

export function pickSemanticFields<T extends object, K extends keyof T>(
  source: T,
  fields: readonly K[],
): Pick<T, K> {
  const result = {} as Pick<T, K>;
  for (const field of fields) {
    if (source[field] !== undefined) result[field] = source[field];
  }
  return result;
}
