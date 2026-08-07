/** 格式化日期时间为「YYYY-MM-DD HH:mm:ss」，无法解析时原样返回 */
export function formatDateTime(value?: string | number | Date | null): string {
  if (value === null || value === undefined || value === '') return '-';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n: number) => String(n).padStart(2, '0');
  const y = date.getFullYear();
  const mo = pad(date.getMonth() + 1);
  const d = pad(date.getDate());
  const h = pad(date.getHours());
  const mi = pad(date.getMinutes());
  const s = pad(date.getSeconds());
  return `${y}-${mo}-${d} ${h}:${mi}:${s}`;
}

/** 统一数据源展示文案：有 ID 时展示「名称（ID）」，否则仅展示名称 */
export function formatDatasourceLabel(name?: string | null, datasourceId?: string | null): string {
  if (!name) return '-';
  return datasourceId ? `${name}（${datasourceId}）` : name;
}

/** 移除 ProTable 传入的分页参数，避免污染业务查询参数 */
export const deleteExtraDelete = <T extends object>(params: T) => {
  const rest = { ...(params as Record<string, unknown>) };
  delete rest.current;
  delete rest.pageSize;
  return rest;
};

export function isFormValidationError(error: unknown): error is { errorFields: unknown[] } {
  return (
    typeof error === 'object' &&
    error !== null &&
    Array.isArray((error as { errorFields?: unknown }).errorFields)
  );
}

export function omitKeys<T extends object, K extends keyof T>(
  value: T,
  keys: readonly K[],
): Omit<T, K> {
  const next = { ...value };
  for (const key of keys) {
    delete next[key];
  }
  return next;
}

export function toOptionalNumber(value: unknown): number | undefined {
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

export function toOptionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined;
}

export function camelToSnake(str: string): string {
  return str.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

export function transformKeysToSnake(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(transformKeysToSnake);
  }
  if (
    obj !== null &&
    typeof obj === 'object' &&
    !(obj instanceof Date) &&
    !(typeof File !== 'undefined' && obj instanceof File) &&
    !(typeof Blob !== 'undefined' && obj instanceof Blob) &&
    !(typeof FormData !== 'undefined' && obj instanceof FormData) &&
    !(typeof URLSearchParams !== 'undefined' && obj instanceof URLSearchParams)
  ) {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      result[camelToSnake(key)] = transformKeysToSnake(value);
    }
    return result;
  }
  return obj;
}
