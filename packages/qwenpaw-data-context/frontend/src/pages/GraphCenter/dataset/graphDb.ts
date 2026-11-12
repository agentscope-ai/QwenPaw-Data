/** Graph query layer — port of cm-graph-demo/js/data.js, rewired to real data.
 *  All queries run over one GraphDataset (fetched from the scoped backend, or
 *  the demo snapshot as an offline fallback). Scoping by viewer/owner happens
 *  server-side (route_security / graph_access), so this module only applies
 *  datasource narrowing on top of the already-authorized dataset. */
import type {
  DomainStat,
  GlobalStats,
  GraphDataset,
  GraphNode,
  HealthIssue,
  HealthIssueCode,
  HealthSummary,
  LayerId,
  NodeType,
  SubgraphData,
} from '../types';

export const TYPE_LABEL: Record<NodeType, string> = {
  domain: '数据域',
  datasource: '数据源',
  dataset: '数据集',
  table: '数据表',
  column: '列',
  datasetfield: '数据集字段',
  metriccaliber: '指标口径',
  verifiedquery: '验证查询',
  operator: '算子',
  metric: '指标',
  dimension: '维度',
  formula: '口径公式',
  caliber: '口径规则',
  task: '任务',
  step: '步骤',
  toolcall: '工具调用',
  session: '会话',
  claim: '断言',
  strategy: '策略卡',
  experience: '经验',
  turn: '对话轮',
  entity: '实体',
  event: '事件',
  memory: '记忆',
  user: '用户',
};

export const LAYER_LABEL: Record<LayerId, string> = {
  MG: '元数据图',
  TG: '任务轨迹',
  KG: '知识图谱',
  UG: '用户记忆',
};

export const LAYERS: LayerId[] = ['MG', 'KG', 'UG'];

const LINEAGE_RELS = new Set(['DERIVES_FROM', 'IN_SOURCE']);

/** 查询词元化：ASCII 词（≥2 字符）+ 汉字二元组。
 *  两个字符都是虚词/疑问助词的二字组（「都有」「什么」）丢弃，
 *  「留存」「指标」这类实义二元组保留。供 search 的 NL 兜底使用。 */
export function tokenizeQuery(kw: string): string[] {
  const tokens: string[] = [];
  const ascii = kw.match(/[a-z0-9_]{2,}/g) || [];
  tokens.push(...ascii);
  const particles = new Set(
    '的了吗呢吧啊哦嘛么是有都在和与跟就还也很不没这那些个为对以到说要看会能什',
  );
  const cjk = kw.replace(/[a-z0-9_\s]/gi, '');
  for (let i = 0; i < cjk.length - 1; i += 1) {
    const bg = cjk.slice(i, i + 2);
    if ([...bg].every((ch) => particles.has(ch))) continue;
    tokens.push(bg);
  }
  return [...new Set(tokens)];
}

export function typeLabel(t: NodeType | string): string {
  return TYPE_LABEL[t as NodeType] || String(t);
}

export function layerLabel(l: LayerId | string): string {
  return LAYER_LABEL[l as LayerId] || String(l);
}

export interface GraphDb {
  dataset: GraphDataset;
  getNode: (id: string) => GraphNode | null;
  degree: (id: string) => number;
  search: (q: string) => GraphNode[];
  domainStats: () => DomainStat[];
  domainList: () => string[];
  subLineage: (id: string) => SubgraphData;
  subImpact: (id: string) => SubgraphData;
  subAssoc: (id: string) => SubgraphData;
  subNeighborhood: (id: string) => SubgraphData;
  relatedItems: (id: string) => GraphNode[];
  healthIssues: () => HealthIssue[];
  healthSummary: () => HealthSummary;
  globalStats: () => GlobalStats;
  datasourceList: () => string[];
  /** Graph-internal datasource id → display name (DataSource anchor labels). */
  datasourceNames: () => Record<string, string>;
  dsForNode: (id: string) => string;
  layerScopeIds: (layer: LayerId, ds: string) => string[];
  scopeHas: (id: string, ds: string) => boolean;
  subFromIds: (idSet: Set<string> | string[]) => SubgraphData;
  layerCenter: (layer: LayerId, ds: string, hop: number, seedId?: string | null) => LayerCenter;
  crossLayerCounts: (ds: string) => Record<string, number>;
}

export interface LayerCenter {
  seed: string | null;
  ids: Set<string>;
  hop: number;
  maxHop: number;
  total: number;
}

/** Build the query API over one dataset. Cheap to rebuild (a few hundred
 *  nodes), so pages regenerate it whenever the dataset changes. */
export function createGraphDb(dataset: GraphDataset): GraphDb {
  const nodes = dataset.nodes;
  const edges = dataset.edges;

  const nodeById = new Map<string, GraphNode>();
  nodes.forEach((n) => nodeById.set(n.id, n));

  const outAdj = new Map<string, { id: string; relation: string }[]>();
  const inAdj = new Map<string, { id: string; relation: string }[]>();
  nodes.forEach((n) => {
    outAdj.set(n.id, []);
    inAdj.set(n.id, []);
  });
  edges.forEach((e) => {
    if (outAdj.has(e.source) && inAdj.has(e.target)) {
      outAdj.get(e.source)!.push({ id: e.target, relation: e.relation });
      inAdj.get(e.target)!.push({ id: e.source, relation: e.relation });
    }
  });

  function getNode(id: string): GraphNode | null {
    return nodeById.get(id) || null;
  }

  function degree(id: string): number {
    return (outAdj.get(id)?.length || 0) + (inAdj.get(id)?.length || 0);
  }

  function search(q: string): GraphNode[] {
    const kw = (q || '').trim().toLowerCase();
    if (!kw) return [];
    const haystack = (n: GraphNode) =>
      `${n.label} ${typeLabel(n.type)} ${n.domain || ''}`.toLowerCase();
    const exact = nodes
      .filter((n) => haystack(n).includes(kw))
      .sort(
        (a, b) =>
          (b.label.toLowerCase().indexOf(kw) === 0 ? 1 : 0) -
          (a.label.toLowerCase().indexOf(kw) === 0 ? 1 : 0),
      );
    if (exact.length) return exact.slice(0, 80);
    // 整串未命中 → 自然语言问句兜底（如「留存都有什么指标」）：按词元重叠
    // 打分，命中词元多的排前面，同分按度数。词元 = ASCII 词 + 汉字二元组。
    const tokens = tokenizeQuery(kw);
    if (!tokens.length) return [];
    return nodes
      .map((n) => {
        const hay = haystack(n);
        let score = 0;
        tokens.forEach((t) => {
          if (hay.includes(t)) score += 1;
        });
        return { n, score };
      })
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score || degree(b.n.id) - degree(a.n.id))
      .slice(0, 80)
      .map((x) => x.n);
  }

  function domainStats(): DomainStat[] {
    const map = new Map<
      string,
      { id: string; label: string; metricCount: number; dsSet: Set<string> }
    >();
    nodes
      .filter((n) => n.type === 'domain')
      .forEach((d) => {
        if (!map.has(d.label))
          map.set(d.label, { id: d.id, label: d.label, metricCount: 0, dsSet: new Set() });
      });
    nodes.forEach((n) => {
      if (n.domain && map.has(n.domain)) {
        const s = map.get(n.domain)!;
        if (n.type === 'metric') s.metricCount++;
        if (n.datasource) s.dsSet.add(n.datasource);
      }
    });
    return [...map.values()].map((s) => ({
      id: s.id,
      label: s.label,
      metricCount: s.metricCount,
      dsCount: s.dsSet.size,
    }));
  }

  function domainList(): string[] {
    const out: string[] = [];
    const seen = new Set<string>();
    nodes.forEach((n) => {
      if (n.type === 'domain' && !seen.has(n.label)) {
        seen.add(n.label);
        out.push(n.label);
      }
    });
    return out;
  }

  function traverse(
    startId: string,
    dir: 'out' | 'in' | 'both',
    relations: Set<string> | null,
    depth: number,
    cap = 40,
  ): SubgraphData {
    const ids = new Set<string>([startId]);
    const se: SubgraphData['edges'] = [];
    const seenEdge = new Set<string>();
    const push = (s: string, t: string, r: string) => {
      const k = `${s}->${t}:${r}`;
      if (!seenEdge.has(k)) {
        seenEdge.add(k);
        se.push({ source: s, target: t, relation: r });
      }
    };
    let frontier = [startId];
    for (let d = 0; d < depth && ids.size < cap; d++) {
      const next: string[] = [];
      frontier.forEach((u) => {
        if (dir === 'out' || dir === 'both') {
          (outAdj.get(u) || []).forEach((e) => {
            if (relations && !relations.has(e.relation)) return;
            push(u, e.id, e.relation);
            if (!ids.has(e.id) && ids.size < cap) {
              ids.add(e.id);
              next.push(e.id);
            }
          });
        }
        if (dir === 'in' || dir === 'both') {
          (inAdj.get(u) || []).forEach((e) => {
            if (relations && !relations.has(e.relation)) return;
            push(e.id, u, e.relation);
            if (!ids.has(e.id) && ids.size < cap) {
              ids.add(e.id);
              next.push(e.id);
            }
          });
        }
      });
      frontier = next;
      if (!next.length) break;
    }
    return {
      nodes: [...ids].map((i) => nodeById.get(i)!).filter(Boolean),
      edges: se,
    };
  }

  function subLineage(id: string): SubgraphData {
    return traverse(id, 'out', LINEAGE_RELS, 3, 40);
  }

  function subImpact(id: string): SubgraphData {
    return traverse(id, 'in', null, 1, 40);
  }

  function subNeighborhood(id: string): SubgraphData {
    return traverse(id, 'both', null, 1, 40);
  }

  function subAssoc(id: string): SubgraphData {
    const start = nodeById.get(id);
    if (!start) return { nodes: [], edges: [] };
    const ids = new Set<string>([id]);
    const se: SubgraphData['edges'] = [];
    (outAdj.get(id) || []).forEach((e) => {
      const o = nodeById.get(e.id);
      if (o && o.layer !== start.layer) {
        ids.add(e.id);
        se.push({ source: id, target: e.id, relation: e.relation });
      }
    });
    (inAdj.get(id) || []).forEach((e) => {
      const o = nodeById.get(e.id);
      if (o && o.layer !== start.layer) {
        ids.add(e.id);
        se.push({ source: e.id, target: id, relation: e.relation });
      }
    });
    return { nodes: [...ids].map((i) => nodeById.get(i)!), edges: se };
  }

  function relatedItems(id: string): GraphNode[] {
    const seen = new Set<string>();
    const out: GraphNode[] = [];
    (inAdj.get(id) || []).concat(outAdj.get(id) || []).forEach((e) => {
      if (!seen.has(e.id)) {
        seen.add(e.id);
        const n = nodeById.get(e.id);
        if (n) out.push(n);
      }
    });
    return out;
  }

  // ──────────── Data health heuristics ────────────
  function healthIssues(): HealthIssue[] {
    const out: HealthIssue[] = [];
    const mk = (n: GraphNode, issue: HealthIssueCode): HealthIssue => ({
      id: n.id,
      label: n.label,
      type: n.type,
      layer: n.layer,
      domain: n.domain || null,
      issue,
      refCount: degree(n.id),
    });
    nodes.forEach((n) => {
      if (n.type === 'metric' && !n.definition) out.push(mk(n, 'missingDef'));
      else if (n.type === 'dimension' && degree(n.id) <= 1) out.push(mk(n, 'unusedDim'));
      else if (n.type === 'entity' && degree(n.id) === 0) out.push(mk(n, 'isolated'));
    });
    return out;
  }

  function healthSummary(): HealthSummary {
    const iss = healthIssues();
    return {
      missingDef: iss.filter((i) => i.issue === 'missingDef').length,
      unusedDim: iss.filter((i) => i.issue === 'unusedDim').length,
      isolated: iss.filter((i) => i.issue === 'isolated').length,
    };
  }

  function globalStats(): GlobalStats {
    const byLayer: Record<LayerId, number> = { MG: 0, TG: 0, KG: 0, UG: 0 };
    nodes.forEach((n) => {
      byLayer[n.layer] += 1;
    });
    return { nodeCount: nodes.length, edgeCount: edges.length, byLayer, meta: dataset.meta };
  }

  // ──────────── Datasource narrowing / center subgraph (global graph page) ────────────
  // The dataset is already scoped by the backend to the caller's
  // tenant/workspace/owner. Chip narrowing is CLOSURE-based, not stamp-based:
  // weave only writes `datasource_id` onto Dataset nodes (shared semantic
  // nodes — Domain/Metric/Dimension/DatasetField/MetricCaliber/Table/Column —
  // carry no stamp by design), so a stamp filter would blank 95%+ of the
  // canvas the moment a chip is selected. Instead each chip shows the graph
  // closure of that datasource: BFS from its DataSource anchor + Datasets,
  // with other datasources' Dataset/DataSource nodes as walls and metrics
  // reachable only through their calibers (a domain must not hand out metrics
  // directly, or other datasources' exclusive metrics would leak in).

  /** Prefer the API datasource ID; graph keys may also contain an owner.
   *  Legacy anchors without that field retain the `dsrc:<id>` fallback. */
  function dsIdOf(n: GraphNode): string {
    if (n.datasource) return n.datasource;
    if (n.type === 'datasource') {
      const key = typeof n.props?.key === 'string' ? n.props.key : '';
      if (key.startsWith('dsrc:')) return key.slice('dsrc:'.length);
      return n.label;
    }
    return '';
  }

  function datasourceList(): string[] {
    const s = new Set<string>();
    nodes.forEach((n) => {
      if (n.layer !== 'MG') return;
      const id = dsIdOf(n);
      if (id) s.add(id);
    });
    return [...s];
  }

  /** Graph-internal id → display-name fallback from the DataSource anchor
   *  nodes (labels equal to the raw id are skipped); the authoritative names
   *  come from the datasource-management list overlaid in the context. */
  function datasourceNames(): Record<string, string> {
    const m: Record<string, string> = {};
    nodes.forEach((n) => {
      if (n.layer !== 'MG' || n.type !== 'datasource') return;
      const id = dsIdOf(n);
      if (id && n.label && n.label !== id) m[id] = n.label;
    });
    return m;
  }

  function inDs(n: GraphNode, ds: string): boolean {
    if (ds === 'all') return true;
    if (n.layer === 'MG') return dsScope(ds).has(n.id);
    if (n.layer === 'TG') return n.datasource ? n.datasource === ds : true;
    return true;
  }

  /** Closure of one datasource's subgraph (see section comment). Cached per
   *  db instance; graphs are small (hundreds of nodes) so BFS is trivial. */
  const dsScopeCache = new Map<string, Set<string>>();
  // 有口径的指标：只能经口径归属进视图；无口径指标是域级词汇（与
  // Dimension 同等），允许经域进入。
  const metricsWithCaliber = new Set<string>();
  edges.forEach((e) => {
    if (
      e.relation === 'HAS' &&
      nodeById.get(e.source)?.type === 'metric' &&
      nodeById.get(e.target)?.type === 'metriccaliber'
    )
      metricsWithCaliber.add(e.source);
  });
  function dsScope(ds: string): Set<string> {
    const hit = dsScopeCache.get(ds);
    if (hit) return hit;
    const scope = new Set<string>();
    const queue: string[] = [];
    nodes.forEach((n) => {
      if (
        n.layer === 'MG' &&
        (n.type === 'datasource' || n.type === 'dataset') &&
        dsIdOf(n) === ds
      ) {
        scope.add(n.id);
        queue.push(n.id);
      }
    });
    while (queue.length) {
      const cur = queue.shift()!;
      const curNode = nodeById.get(cur);
      if (!curNode) continue;
      const step = (otherId: string, relation: string) => {
        if (scope.has(otherId)) return;
        const other = nodeById.get(otherId);
        if (!other || other.layer !== 'MG') return;
        // 其他数据源的 Dataset/DataSource 是墙：不进入、不穿越。
        if ((other.type === 'dataset' || other.type === 'datasource') && dsIdOf(other) !== ds)
          return;
        // 域不直接发"有口径"指标（否则别的源独占指标会漏进来）；
        // 无口径指标按域级词汇放行。
        if (
          relation === 'HAS' &&
          ((curNode.type === 'domain' && other.type === 'metric') ||
            (curNode.type === 'metric' && other.type === 'domain'))
        ) {
          const metricId = curNode.type === 'domain' ? otherId : cur;
          if (metricsWithCaliber.has(metricId)) return;
        }
        // 指标不回撒口径：避免把别的源上的口径节点带进本源视图。
        if (relation === 'HAS' && curNode.type === 'metric' && other.type === 'metriccaliber')
          return;
        scope.add(otherId);
        queue.push(otherId);
      };
      (outAdj.get(cur) || []).forEach((e) => step(e.id, e.relation));
      (inAdj.get(cur) || []).forEach((e) => step(e.id, e.relation));
    }
    dsScopeCache.set(ds, scope);
    return scope;
  }

  function layerScopeIds(layer: LayerId, ds: string): string[] {
    return nodes.filter((n) => n.layer === layer && inDs(n, ds)).map((n) => n.id);
  }

  function scopeHas(id: string, ds: string): boolean {
    const n = nodeById.get(id);
    return !!n && inDs(n, ds);
  }

  function pickSeed(ids: string[]): string | null {
    let best: string | null = ids[0] || null;
    let bd = -1;
    ids.forEach((i) => {
      const d = degree(i);
      if (d > bd) {
        bd = d;
        best = i;
      }
    });
    return best;
  }

  function bfsLevels(
    seedId: string,
    allowed: Set<string>,
  ): { levels: Map<string, number>; maxHop: number } {
    if (!allowed.has(seedId)) return { levels: new Map(), maxHop: 0 };
    const levels = new Map<string, number>();
    levels.set(seedId, 0);
    let currentLevel = [seedId];
    let hop = 0;
    while (currentLevel.length > 0) {
      const nextLevel: string[] = [];
      for (const u of currentLevel) {
        const nb = (outAdj.get(u) || []).concat(inAdj.get(u) || []);
        for (const e of nb) {
          if (allowed.has(e.id) && !levels.has(e.id)) {
            levels.set(e.id, hop + 1);
            nextLevel.push(e.id);
          }
        }
      }
      if (nextLevel.length > 0) hop++;
      currentLevel = nextLevel;
    }
    // 层内不相连的点放到下一跳，否则 maxHop=0，扩图加减号会一起禁用。
    let isolated = false;
    allowed.forEach((id) => {
      if (!levels.has(id)) {
        levels.set(id, hop + 1);
        isolated = true;
      }
    });
    return { levels, maxHop: isolated ? hop + 1 : hop };
  }

  function idsAtHop(levels: Map<string, number>, hop: number): Set<string> {
    const ids = new Set<string>();
    levels.forEach((lvl, id) => {
      if (lvl <= hop) ids.add(id);
    });
    return ids;
  }

  function subFromIds(idSet: Set<string> | string[]): SubgraphData {
    const set = idSet instanceof Set ? idSet : new Set(idSet);
    const se: SubgraphData['edges'] = [];
    const seen = new Set<string>();
    edges.forEach((e) => {
      if (set.has(e.source) && set.has(e.target)) {
        const k = e.source + '>' + e.target + e.relation;
        if (!seen.has(k)) {
          seen.add(k);
          se.push(e);
        }
      }
    });
    return { nodes: [...set].map((i) => nodeById.get(i)!).filter(Boolean), edges: se };
  }

  function layerCenter(
    layer: LayerId,
    ds: string,
    hop: number,
    seedId?: string | null,
  ): LayerCenter {
    const ids = layerScopeIds(layer, ds);
    if (!ids.length) return { seed: null, ids: new Set(), hop: 0, maxHop: 0, total: 0 };
    const allowed = new Set(ids);
    const seed = seedId && allowed.has(seedId) ? seedId : pickSeed(ids);
    if (!seed) return { seed: null, ids: new Set(), hop: 0, maxHop: 0, total: ids.length };
    const r = bfsLevels(seed, allowed);
    const actualHop = Math.min(hop, r.maxHop);
    const included = idsAtHop(r.levels, actualHop);
    return { seed, ids: included, hop: actualHop, maxHop: r.maxHop, total: ids.length };
  }

  function crossLayerCounts(ds: string): Record<string, number> {
    const scope = new Set(nodes.filter((n) => inDs(n, ds)).map((n) => n.id));
    const counts: Record<string, number> = {};
    edges.forEach((e) => {
      const a = nodeById.get(e.source);
      const b = nodeById.get(e.target);
      if (!a || !b || a.layer === b.layer) return;
      if (!scope.has(e.source) || !scope.has(e.target)) return;
      const key = [a.layer, b.layer].sort().join('↔');
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }

  function dsForNode(id: string): string {
    const n = nodeById.get(id);
    if (!n) return 'all';
    const stamped = dsIdOf(n);
    if (stamped) return stamped;
    // 无戳节点（共享语义层）：定位到第一个闭包包含它的数据源。
    if (n.layer === 'MG') {
      for (const ds of datasourceList()) {
        if (dsScope(ds).has(id)) return ds;
      }
    }
    return 'all';
  }

  return {
    dataset,
    getNode,
    degree,
    search,
    domainStats,
    domainList,
    subLineage,
    subImpact,
    subAssoc,
    subNeighborhood,
    relatedItems,
    healthIssues,
    healthSummary,
    globalStats,
    datasourceList,
    datasourceNames,
    dsForNode,
    layerScopeIds,
    scopeHas,
    subFromIds,
    layerCenter,
    crossLayerCounts,
  };
}

// ──────────── Owner helpers (real-scope visual hints) ────────────
// The backend write path stamps every node with workspace_id/owner_id; the
// mirror-add visual (dashed outline) keys off that stamp instead of the old
// simulated viewer model.

/** Owner stamp carried by backend nodes (null when absent, e.g. demo data). */
export function nodeOwnerId(n: GraphNode): string | null {
  const o = n.props?.owner_id;
  return typeof o === 'string' && o ? o : null;
}

/** Member mirror-add: owner-scoped node layered over the global MG copy. */
export function isPrivateNode(n: GraphNode): boolean {
  const o = nodeOwnerId(n);
  return o !== null && o !== '__global__';
}

export interface CypherRow {
  id: string;
  label: string;
  layer: LayerId;
  type: NodeType;
}

/** Offline Cypher simulation used when the backend is unreachable (demo
 *  fallback): naive `:Label` filter over the loaded dataset. */
export function simulateCypher(dataset: GraphDataset, q: string): CypherRow[] {
  const m = /:(\w+)/.exec(q || '');
  let rows = dataset.nodes;
  if (m) {
    const t = m[1].toLowerCase();
    rows = dataset.nodes.filter((n) => n.type === t);
    if (!rows.length) rows = dataset.nodes;
  }
  return rows.slice(0, 20).map((n) => ({ id: n.id, label: n.label, layer: n.layer, type: n.type }));
}
