/** Small fetch helpers for dashboard widgets — real endpoints first, null on
 *  any failure so widgets fall back to dataset-derived or demo data. */
import { request } from '@/services/request';

export interface MgSyncStatus {
  space_status: 'uninitialized' | 'initializing' | 'ready';
  global_digest: string;
  last_synced_at: string | null;
  needs_sync: boolean;
}

/** GET /api/v1/mg/userspace/sync-status — every tenant role owns a userspace.
 *  redirectOnForbidden=false: this is a widget probe, so an unexpected 403
 *  must degrade to the null fallback below, never hard-redirect the whole
 *  app to /403 (the shared request layer's default behavior). */
export async function fetchMgSyncStatus(): Promise<MgSyncStatus | null> {
  try {
    const payload = await request<{ ok: boolean; data: MgSyncStatus | null } | MgSyncStatus>(
      '/v1/mg/userspace/sync-status',
      { redirectOnForbidden: false },
    );
    if (payload && typeof payload === 'object' && 'ok' in payload) {
      return payload.ok ? payload.data : null;
    }
    return payload as MgSyncStatus;
  } catch {
    return null;
  }
}
