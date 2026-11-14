import type { DataSourceConnectionTestResult } from '@/types/dataSource';

export function unwrapConnectionTestResult(raw: unknown): DataSourceConnectionTestResult {
  const root = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const nested =
    root.data && typeof root.data === 'object' ? (root.data as Record<string, unknown>) : root;
  return {
    success: nested.success === true,
    message: typeof nested.message === 'string' ? nested.message : '',
    tables_found: typeof nested.tables_found === 'number' ? nested.tables_found : null,
    elapsed_ms: typeof nested.elapsed_ms === 'number' ? nested.elapsed_ms : 0,
  };
}
