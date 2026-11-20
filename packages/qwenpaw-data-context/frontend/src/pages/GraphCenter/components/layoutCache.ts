/** Session + in-memory cache for the global graph's per-layer force layout.
 *  Exact fingerprint hits skip forceCluster entirely (page refresh / reload).
 *  Partial hits warm-start the solver so hop expand/shrink stays stable. */
import type { LayerId } from '../types';
import type { Point } from './forceLayout';

export type LayerBBox = { w: number; h: number };

export interface LayoutSnapshot {
  fingerprint: string;
  localPos: Record<LayerId, Record<string, Point>>;
  bbox: Record<LayerId, LayerBBox>;
}

const LAYERS: LayerId[] = ['MG', 'KG', 'UG'];

export function idsFingerprint(sets: Record<LayerId, Iterable<string>>): string {
  return LAYERS.map((L) => [...(sets[L] || [])].sort().join(',')).join('|');
}

export function layoutCacheKey(
  workspaceId: string,
  datasourceId: string,
  hopKey: string,
  mode = 'dev',
): string {
  return `gc-mgraph-layout:v2:${workspaceId || '_'}:${datasourceId}:${mode}:${hopKey}`;
}

export function loadLayout(key: string): LayoutSnapshot | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as LayoutSnapshot;
    if (!parsed?.fingerprint || !parsed.localPos || !parsed.bbox) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveLayout(key: string, snap: LayoutSnapshot): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(snap));
  } catch {
    /* quota / private mode */
  }
}
