/** 统一解析分页/列表接口响应 */
export function normalizeListResponse<T = unknown>(result: unknown): T[] {
  if (Array.isArray(result)) return result as T[];
  if (result && typeof result === 'object') {
    const data = result as Record<string, unknown>;
    if (Array.isArray(data.records)) return data.records as T[];
    if (Array.isArray(data.data)) return data.data as T[];
    if (Array.isArray(data.content)) return data.content as T[];
  }
  return [];
}

/** 从分页响应中提取 total 总数 */
export function extractTotal(result: unknown): number {
  if (result && typeof result === 'object') {
    const data = result as Record<string, unknown>;
    if (typeof data.total === 'number') return data.total;
  }
  return 0;
}

export type SelectOption<V extends string | number = number> = {
  label: string;
  value: V;
};
