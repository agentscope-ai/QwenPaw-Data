/** Dashboard stat derivations — pure functions over one GraphDb instance.
 *  Real backend signals (MG sync) are fetched by the widgets
 *  themselves; everything here works over the scoped dataset so the same
 *  widgets render identically in "api" and offline "demo" mode. */
import type { GraphDb } from './graphDb';
import type { GraphNode, LayerId } from '../types';
import { LAYERS } from './graphDb';

// ────────────────────────────────────────────────────────────────────────
// 1 · KPI strip
// ────────────────────────────────────────────────────────────────────────

export interface KpiStats {
  nodeCount: number;
  edgeCount: number;
  issueCount: number;
}

export function deriveKpis(db: GraphDb): KpiStats {
  const stats = db.globalStats();
  const issues = db.healthSummary();
  return {
    nodeCount: stats.nodeCount,
    edgeCount: stats.edgeCount,
    issueCount: issues.missingDef + issues.unusedDim + issues.isolated,
  };
}

// ────────────────────────────────────────────────────────────────────────
// 3 · Layer distribution (rose)
// ────────────────────────────────────────────────────────────────────────

export interface LayerSlice {
  layer: LayerId;
  nodes: number;
  edges: number;
}

export function deriveLayerDistribution(db: GraphDb): LayerSlice[] {
  const stats = db.globalStats();
  const edgeByLayer: Record<LayerId, number> = { MG: 0, TG: 0, KG: 0, UG: 0 };
  db.dataset.edges.forEach((e) => {
    const src = db.getNode(e.source);
    if (src) edgeByLayer[src.layer] += 1;
  });
  return LAYERS.map((layer) => ({
    layer,
    nodes: stats.byLayer[layer] ?? 0,
    edges: edgeByLayer[layer],
  }));
}

// ────────────────────────────────────────────────────────────────────────
// 5 · Data health
// ────────────────────────────────────────────────────────────────────────

export interface HealthStats {
  score: number; // 0..100 overall connectivity/quality score
  missingDef: number;
  unusedDim: number;
  isolated: number;
}

export function deriveHealth(db: GraphDb): HealthStats {
  const s = db.healthSummary();
  const total = db.globalStats().nodeCount || 1;
  const issueTotal = s.missingDef + s.unusedDim + s.isolated;
  const score = Math.max(0, Math.round(100 * (1 - issueTotal / Math.max(total * 0.35, 1))));
  return {
    score: Math.min(score, 100),
    missingDef: s.missingDef,
    unusedDim: s.unusedDim,
    isolated: s.isolated,
  };
}

// ────────────────────────────────────────────────────────────────────────
// 7 · Hotspots (top-degree nodes)
// ────────────────────────────────────────────────────────────────────────

export interface Hotspot {
  node: GraphNode;
  degree: number;
}

export function deriveHotspots(db: GraphDb, limit = 8): Hotspot[] {
  return db.dataset.nodes
    .map((node) => ({ node, degree: db.degree(node.id) }))
    .sort((a, b) => b.degree - a.degree)
    .slice(0, limit);
}

// ────────────────────────────────────────────────────────────────────────
// 9 · Recently curated assets (MG metrics/dimensions)
// ────────────────────────────────────────────────────────────────────────

export interface CuratedAsset {
  node: GraphNode;
  kind: 'metric' | 'dimension';
  createdAt: string | null; // ISO date or null when unknown (demo snapshot)
}

export function deriveCuratedAssets(db: GraphDb, limit = 8): CuratedAsset[] {
  const candidates = db.dataset.nodes.filter(
    (n) => n.layer === 'MG' && (n.type === 'metric' || n.type === 'dimension'),
  );

  const assets: CuratedAsset[] = candidates.map((node) => {
    const rawCreated = node.props?.created_at ?? node.props?.curated_at;
    return {
      node,
      kind: node.type === 'metric' ? 'metric' : 'dimension',
      createdAt: typeof rawCreated === 'string' && rawCreated ? rawCreated.slice(0, 10) : null,
    };
  });

  // Real data sorts by creation date; unknown dates sort by key.
  assets.sort((a, b) => {
    if (a.createdAt && b.createdAt) return b.createdAt.localeCompare(a.createdAt);
    if (a.createdAt || b.createdAt) return a.createdAt ? -1 : 1;
    return a.node.id.localeCompare(b.node.id);
  });

  return assets.slice(0, limit);
}

// ────────────────────────────────────────────────────────────────────────
// 8 · Audit feed (demo derivation; real endpoint is a v2 follow-up)
// ────────────────────────────────────────────────────────────────────────

export type AuditDemoKind = 'trace' | 'claim' | 'sync' | 'tool' | 'userspace' | 'memory';

export interface AuditEvent {
  id: string;
  /** Layer hue key for the timeline dot. */
  layer: LayerId;
  kind: AuditDemoKind;
  count?: number;
  time: string;
}

/** Synthesize a clearly-labeled demo feed from dataset structure. */
export function deriveDemoAudit(db: GraphDb, limit = 8): AuditEvent[] {
  const events: AuditEvent[] = [];
  const push = (layer: LayerId, kind: AuditDemoKind, minsAgo: number, count?: number) => {
    const d = new Date(Date.now() - minsAgo * 60000);
    events.push({
      id: `demo-${events.length}`,
      layer,
      kind,
      count,
      time: `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`,
    });
  };

  const tasks = db.dataset.nodes.filter((n) => n.type === 'task').length;
  const claims = db.dataset.nodes.filter((n) => n.type === 'claim').length;
  const metrics = db.dataset.nodes.filter((n) => n.type === 'metric').length;
  const toolcalls = db.dataset.nodes.filter((n) => n.type === 'toolcall').length;

  if (tasks) push('TG', 'trace', 42, tasks);
  if (claims) push('TG', 'claim', 96, claims);
  if (metrics) push('MG', 'sync', 187, metrics);
  if (toolcalls) push('TG', 'tool', 320, toolcalls);
  push('MG', 'userspace', 1560);
  push('UG', 'memory', 2880);

  return events.slice(0, limit);
}
