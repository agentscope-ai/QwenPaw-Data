/** Path-steered reasoning journey — demo engine over the loaded GraphDb.
 *
 *  Same shape of story as the concept-k-search-journey prototype, but walked
 *  over the REAL dataset: anchor = best search hit; candidate branches = the
 *  anchor's neighborhood grouped by path family; one branch is picked by a
 *  relevance heuristic and its nodes are collected into the context pack.
 *
 *  When nothing matches the query we fall back to a clearly-labeled canned
 *  demo story (demoStory = true) so the demo always plays. The backend
 *  contract for a future real wiring is the search_context SSE stream
 *  (progress events: anchors / decision / traversal / assemble). */
import type { GraphNode, LayerId } from '../types';
import { tokenizeQuery, typeLabel } from './graphDb';
import type { GraphDb } from './graphDb';

export const LAYER_JOURNEY_COLOR: Record<LayerId, string> = {
  MG: '#0ea5e9',
  TG: '#8b5cf6',
  KG: '#10b981',
  UG: '#f59e0b',
};

export interface JourneyEdge {
  from: string;
  to: string;
  relation: string;
}

export interface JourneyNodeRef {
  id: string;
  label: string;
  layer: LayerId;
  type: string;
}

export interface JourneyBranch {
  id: string;
  title: string;
  color: string;
  verdict: 'chosen' | 'rejected';
  score: number;
  reason: string;
  nodes: JourneyNodeRef[];
  edges: JourneyEdge[];
}

export interface JourneyContextItem {
  nodeId: string | null;
  kind: string;
  label: string;
  layer: LayerId | null;
  color: string;
}

export interface Journey {
  query: string;
  anchor: JourneyNodeRef | null;
  branches: JourneyBranch[];
  context: JourneyContextItem[];
  conclusion: string;
  narrations: string[];
  demoStory: boolean;
  /** Dataset never loaded (no session / backend down) — show a real empty
   *  state instead of disguising it as the canned story. */
  noData: boolean;
  /** Data exists but the query matched nothing (or nothing walkable). */
  noHit: boolean;
  suggestions: string[];
}

type FamilyKey = 'lineage' | 'caliber' | 'semantic' | 'knowledge' | 'trace' | 'memory' | 'other';

const FAMILY_META: Record<FamilyKey, { title: string; color: string; prior: number }> = {
  lineage: { title: '数据源链路', color: '#10b981', prior: 0.8 },
  caliber: { title: '口径与公式', color: '#0ea5e9', prior: 0.85 },
  semantic: { title: '指标语义邻域', color: '#6366f1', prior: 0.72 },
  knowledge: { title: '知识事件', color: '#14b8a6', prior: 0.7 },
  trace: { title: '任务轨迹', color: '#f59e0b', prior: 0.55 },
  memory: { title: '用户记忆', color: '#d97706', prior: 0.5 },
  other: { title: '其他关联', color: '#64748b', prior: 0.45 },
};

const KIND_BY_TYPE: Record<string, string> = {
  metric: 'Metric',
  table: 'Table',
  dataset: 'Table',
  column: 'Column',
  formula: 'Formula',
  caliber: 'Caliber',
  dimension: 'Dimension',
  event: 'Event',
  entity: 'Entity',
  experience: 'Experience',
  strategy: 'Experience',
  task: 'Trace',
  step: 'Trace',
  toolcall: 'Trace',
  session: 'Trace',
  turn: 'Trace',
  memory: 'Memory',
  user: 'Memory',
  claim: 'Claim',
  domain: 'Domain',
  datasource: 'Source',
};

function familyOf(n: GraphNode): FamilyKey {
  if (n.type === 'formula' || n.type === 'caliber') return 'caliber';
  if (n.layer === 'TG') return 'trace';
  if (n.layer === 'UG') return 'memory';
  if (n.layer === 'KG') return 'knowledge';
  if (['table', 'column', 'dataset', 'datasource'].includes(n.type)) return 'lineage';
  if (['metric', 'dimension'].includes(n.type)) return 'semantic';
  return 'other';
}

function queryTokens(q: string): string[] {
  return tokenizeQuery((q || '').trim().toLowerCase());
}

function overlapScore(tokens: string[], nodes: JourneyNodeRef[]): number {
  if (!tokens.length || !nodes.length) return 0;
  let hit = 0;
  const hay = nodes.map((n) => n.label.toLowerCase());
  tokens.forEach((t) => {
    if (hay.some((h) => h.includes(t))) hit += 1;
  });
  return hit / tokens.length;
}

function ref(n: GraphNode): JourneyNodeRef {
  return { id: n.id, label: n.label, layer: n.layer, type: n.type };
}

/** Walk the real graph. The canned story only plays when explicitly
 *  requested (opts.forceDemo); misses surface as an honest noHit state. */
export function runJourney(db: GraphDb, q: string, opts?: { forceDemo?: boolean }): Journey {
  if (opts?.forceDemo) return cannedJourney(q);
  if (!db.dataset.nodes.length) return noDataJourney(q);
  const hits = db.search(q);
  const anchorNode = hits.find((n) => n.type !== 'domain') || hits[0];
  if (!anchorNode) return noHitJourney(db, q, null);

  const anchor = ref(anchorNode);
  const tokens = queryTokens(q);
  const hood = db.subNeighborhood(anchorNode.id);
  const anchorId = anchorNode.id;

  // group direct neighbors into path families
  const famNodes = new Map<FamilyKey, JourneyNodeRef[]>();
  const famEdges = new Map<FamilyKey, JourneyEdge[]>();
  hood.nodes.forEach((n) => {
    if (n.id === anchorId) return;
    const f = familyOf(n);
    if (!famNodes.has(f)) {
      famNodes.set(f, []);
      famEdges.set(f, []);
    }
    famNodes.get(f)!.push(ref(n));
  });
  hood.edges.forEach((e) => {
    if (e.source !== anchorId && e.target !== anchorId) return;
    const otherId = e.source === anchorId ? e.target : e.source;
    const other = db.getNode(otherId);
    if (!other) return;
    const f = familyOf(other);
    famEdges.get(f)?.push({ from: e.source, to: e.target, relation: e.relation });
  });

  // rank families → up to 3 branches
  const ranked = [...famNodes.entries()]
    .filter(([, ns]) => ns.length > 0)
    .map(([fam, ns]) => {
      const meta = FAMILY_META[fam];
      const ov = overlapScore(tokens, ns);
      const size = Math.min(1, ns.length / 4);
      const score = Math.min(
        0.99,
        Math.round((meta.prior * 0.7 + ov * 0.22 + size * 0.08) * 100) / 100,
      );
      return { fam, nodes: ns, edges: famEdges.get(fam) || [], score, ov };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, 3);

  if (!ranked.length) return noHitJourney(db, q, anchor);

  // expand the best branch one hop deeper so the path actually goes somewhere
  const best = ranked[0];
  const expandIds = best.nodes.slice(0, 2).map((n) => n.id);
  const seen = new Set<string>([anchorId, ...best.nodes.map((n) => n.id)]);
  expandIds.forEach((nid) => {
    const deeper = db.subNeighborhood(nid);
    deeper.nodes.forEach((n) => {
      if (seen.has(n.id) || best.nodes.length >= 7) return;
      seen.add(n.id);
      best.nodes.push(ref(n));
    });
    deeper.edges.forEach((e) => {
      if (seen.has(e.source) && seen.has(e.target)) {
        best.edges.push({ from: e.source, to: e.target, relation: e.relation });
      }
    });
  });

  const letters = ['A', 'B', 'C'];
  const branches: JourneyBranch[] = ranked.map((r, i) => {
    const meta = FAMILY_META[r.fam];
    const chosen = i === 0;
    let reason: string;
    if (chosen) reason = '覆盖问题实体与口径链，相关度最高';
    else if (r.ov === 0) reason = '与问题词面无交集';
    else if (r.nodes.length <= 1) reason = '邻域过小，信息量不足';
    else reason = '相关度低于选中路';
    return {
      id: letters[i],
      title: meta.title,
      color: meta.color,
      verdict: chosen ? 'chosen' : 'rejected',
      score: r.score,
      reason,
      nodes: r.nodes,
      edges: r.edges,
    };
  });

  const chosenBranch = branches[0];
  const ctxSource = [anchor, ...chosenBranch.nodes];
  const usedKinds = new Set<string>();
  const context: JourneyContextItem[] = [];
  ctxSource.forEach((n) => {
    if (context.length >= 6) return;
    const kind = KIND_BY_TYPE[n.type] || typeLabel(n.type);
    const key = kind + n.label;
    if (usedKinds.has(key)) return;
    usedKinds.add(key);
    context.push({
      nodeId: n.id,
      kind,
      label: n.label,
      layer: n.layer,
      color: LAYER_JOURNEY_COLOR[n.layer],
    });
  });

  // Objective summary of what the path actually produced — no editorializing.
  const coverage = context
    .slice(0, 4)
    .map((c) => c.kind + '·' + c.label)
    .join('、');
  const conclusion =
    '择路结果:路 ' +
    chosenBranch.id +
    '「' +
    chosenBranch.title +
    '」(相关度 ' +
    chosenBranch.score.toFixed(2) +
    ')' +
    ';沿该路径收集 ' +
    context.length +
    ' 条上下文' +
    (coverage ? ',覆盖:' + coverage : '');
  const narrations = [
    '收到查询：<b>' + q + '</b>',
    '锚点入图：过闸锚点 <b>' + anchor.label + '</b>',
    '前沿扩展：长出 <b>' + branches.length + ' 条候选路</b>',
    '给每条路打相关度：<b>哪条能把推理带上正路？</b>',
    '择路：走 <b>路 ' + chosenBranch.id + '（' + chosenBranch.title + '）</b>，其余被拒',
    '选中路径节点 → <b>收集成上下文包</b>',
    '上下文包就绪（' + context.length + ' 条）—— 喂给模型的料',
    '结论：' + conclusion,
  ];

  return {
    query: q,
    anchor,
    branches,
    context,
    conclusion,
    narrations,
    demoStory: false,
    noData: false,
    noHit: false,
    suggestions: [],
  };
}

/* ── honest miss: data exists, query matched nothing walkable ───────── */
function noHitJourney(db: GraphDb, q: string, matched: JourneyNodeRef | null): Journey {
  const suggestions = db.dataset.nodes
    .filter((n) => n.type !== 'domain')
    .sort((a, b) => db.degree(b.id) - db.degree(a.id))
    .slice(0, 6)
    .map((n) => n.label);
  return {
    query: q,
    anchor: matched,
    branches: [],
    context: [],
    conclusion: '',
    narrations: [],
    demoStory: false,
    noData: false,
    noHit: true,
    suggestions,
  };
}

/* ── empty dataset: not logged in / backend unreachable ─────────────── */
function noDataJourney(q: string): Journey {
  return {
    query: q,
    anchor: null,
    branches: [],
    context: [],
    conclusion: '',
    narrations: [],
    demoStory: false,
    noData: true,
    noHit: false,
    suggestions: [],
  };
}

/* ── canned demo story (prototype K 的 DAU 剧本) ─────────────────────── */
function cannedJourney(q: string): Journey {
  const N = (id: string, label: string, layer: LayerId, type: string): JourneyNodeRef => ({
    id,
    label,
    layer,
    type,
  });
  const dau = N('demo:dau', '当日访问用户数', 'MG', 'metric');
  const branches: JourneyBranch[] = [
    {
      id: 'A',
      title: '口径与数据源链路',
      color: '#10b981',
      verdict: 'chosen',
      score: 0.86,
      reason: '覆盖问题实体与口径链，相关度最高',
      nodes: [
        N('demo:fml', '公式·日活', 'MG', 'formula'),
        N('demo:tbl', 'dws_visit_df', 'MG', 'table'),
        N('demo:col', 'country 列', 'MG', 'column'),
        N('demo:ev', '事件·模型版本发布', 'KG', 'event'),
        N('demo:card', '经验卡·剔除测试账号', 'TG', 'experience'),
      ],
      edges: [
        { from: 'demo:dau', to: 'demo:fml', relation: 'HAS_FORMULA' },
        { from: 'demo:fml', to: 'demo:tbl', relation: 'OF_VIEW' },
        { from: 'demo:tbl', to: 'demo:col', relation: 'HAS' },
        { from: 'demo:ev', to: 'demo:dau', relation: 'OBSERVES' },
        { from: 'demo:card', to: 'demo:dau', relation: 'CARD_META' },
      ],
    },
    {
      id: 'B',
      title: '月活口径',
      color: '#f59e0b',
      verdict: 'rejected',
      score: 0.37,
      reason: '口径不符：MAU 是月度口径',
      nodes: [N('demo:mau', 'MAU', 'MG', 'metric')],
      edges: [{ from: 'demo:dau', to: 'demo:mau', relation: 'DERIVED_FROM' }],
    },
    {
      id: 'C',
      title: '分享页表面指标',
      color: '#ef4444',
      verdict: 'rejected',
      score: 0.34,
      reason: '与问题实体无关',
      nodes: [N('demo:share', '分享页访问用户数', 'MG', 'metric')],
      edges: [{ from: 'demo:dau', to: 'demo:share', relation: 'SURFACE_METRIC' }],
    },
  ];
  const context: JourneyContextItem[] = [
    {
      nodeId: 'demo:dau',
      kind: 'Metric',
      label: '当日访问用户数（DAU）',
      layer: 'MG',
      color: '#0ea5e9',
    },
    {
      nodeId: 'demo:fml',
      kind: 'Formula',
      label: '日活 = count(distinct user_id)',
      layer: 'MG',
      color: '#10b981',
    },
    {
      nodeId: 'demo:tbl',
      kind: 'Table',
      label: 'dws_visit_df（ds / country / user_id）',
      layer: 'MG',
      color: '#6366f1',
    },
    {
      nodeId: 'demo:ev',
      kind: 'Event',
      label: '模型版本发布 @T-2 · OBSERVES→DAU',
      layer: 'KG',
      color: '#f59e0b',
    },
    {
      nodeId: 'demo:card',
      kind: 'Experience',
      label: '剔除内部测试账号 / 免登用户',
      layer: 'TG',
      color: '#8b5cf6',
    },
  ];
  const conclusion = '归因 = 版本发布 × 区域维度 → 输出正确分析路径（relevance 0.86）';
  return {
    query: q,
    anchor: dau,
    branches,
    context,
    conclusion,
    narrations: [
      '收到查询：<b>' + q + '</b>',
      '当前数据集没有匹配节点 → 播放<b>演示剧本</b>（DAU 归因）',
      '锚点入图：<b>当日访问用户数</b>',
      '前沿扩展：长出 <b>3 条候选路</b> —— 绿(正确) / 黄(低相关) / 红(错误)',
      '择路：走 <b>路 A</b>；MAU(口径不符) 与 分享页(无关) 被拒',
      '选中路径节点 → <b>收集成上下文包（5 条）</b>',
      '结论：' + conclusion,
    ],
    demoStory: true,
    noData: false,
    noHit: false,
    suggestions: [],
  };
}
