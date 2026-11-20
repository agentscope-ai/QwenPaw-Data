// ============================================================
// Userspace MG Sync API — self-scope pull + append (member / ws_admin)
// Endpoints live under /api/v1/mg/userspace (NOT /v1/admin).
// ============================================================

import { buildAuthHeaders } from '../authHeaders';
import { getApiUrl } from '../config';
import { parseApiError } from '../apiError';
import type { ApiResponse } from './types';

export interface MgSyncStatus {
  space_status: 'uninitialized' | 'initializing' | 'ready';
  global_digest: string;
  last_synced_at: string | null;
  needs_sync: boolean;
}

export interface MgConflict {
  kind: 'node';
  label: string;
  key: string;
  global_props: Record<string, unknown>;
  user_props: Record<string, unknown>;
}

export interface MgSyncResult {
  global_digest: string;
  created: number;
  overwritten: number;
  deleted: number;
  auto_adopted: number;
  conflict_count: number;
  conflicts: MgConflict[];
}

export interface MgResolution {
  kind: 'node';
  label: string;
  key: string;
  decision: 'global' | 'user';
}

export interface MgResolveResult {
  resolved: number;
  synced: boolean;
}

/** HTTP error preserving the status code (409 = global MG changed, re-pull). */
export class MgSyncHttpError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'MgSyncHttpError';
    this.status = status;
  }
}

async function mgRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.method === 'POST' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  for (const [key, value] of Object.entries(buildAuthHeaders())) {
    if (!headers.has(key)) headers.set(key, value);
  }
  const response = await fetch(getApiUrl(`/v1/mg/userspace${path}`), {
    ...options,
    credentials: 'include',
    headers,
  });
  const text = await response.text().catch(() => '');
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new MgSyncHttpError(
      response.status,
      parseApiError(payload, response.status, response.statusText).message,
    );
  }
  const envelope = payload as ApiResponse<T> | null;
  if (envelope && typeof envelope === 'object' && 'ok' in envelope) {
    if (!envelope.ok) {
      throw new MgSyncHttpError(response.status, envelope.error?.message || 'Request failed');
    }
    return envelope.data as T;
  }
  return payload as T;
}

export function fetchMgSyncStatus(): Promise<MgSyncStatus> {
  return mgRequest<MgSyncStatus>('/sync-status');
}

export function syncUserspaceMg(): Promise<MgSyncResult> {
  return mgRequest<MgSyncResult>('/sync', { method: 'POST' });
}

export function resolveMgSync(
  globalDigest: string,
  resolutions: MgResolution[],
): Promise<MgResolveResult> {
  return mgRequest<MgResolveResult>('/sync/resolve', {
    method: 'POST',
    body: JSON.stringify({ global_digest: globalDigest, resolutions }),
  });
}

export function appendMgNode(
  label: string,
  key: string,
  props: Record<string, unknown>,
): Promise<{ label: string; key: string }> {
  return mgRequest('/nodes', {
    method: 'POST',
    body: JSON.stringify({ label, key, props }),
  });
}

export function appendMgEdge(
  fromKey: string,
  toKey: string,
  relType: string,
  props: Record<string, unknown>,
): Promise<{ from_key: string; rel_type: string; to_key: string }> {
  return mgRequest('/edges', {
    method: 'POST',
    body: JSON.stringify({ from_key: fromKey, to_key: toKey, rel_type: relType, props }),
  });
}

/** MG label whitelist — must stay 1:1 with backend MG_SYNC_LABELS. */
export const MG_APPEND_LABELS = [
  'DataSource',
  'Domain',
  'Metric',
  'Formula',
  'Dimension',
  'DimensionValue',
  'Caliber',
  'Dataset',
  'DatasetColumn',
  'Operator',
  'Database',
  'Schema',
  'Table',
  'Column',
] as const;
