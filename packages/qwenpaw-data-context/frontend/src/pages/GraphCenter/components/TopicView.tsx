/** Topic graph: metadata, knowledge and user layers. */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/design';
import type { Topic, TopicMember } from '@/services/cmgraph';
import { forceCluster, type Point } from './forceLayout';
import styles from './TopicView.module.less';

// ── light aurora palette ──────────────────────────────────────────────
const BG_TOP = '#ffffff';
const BG_BOT = '#edf0fb';
const HAIR = 'rgba(79,82,217,0.14)';
const HAIR_STRONG = 'rgba(79,82,217,0.38)';
const TXT = '#4a4f8f';
const TXT_DIM = '#8a90b8';
const MONO = '"SF Mono", ui-monospace, Menlo, Consolas, monospace';

const GRAPH_COLOR: Record<string, string> = {
  MG: '#0ea5e9', // sky
  KG: '#10b981', // emerald
  UG: '#f59e0b', // amber
  TG: '#8b5cf6', // violet
};
const DISTILL = '#ec4899'; // pink for learned tier

// semantic labels — keys/ids stay in the detail drill-down only（仅中文界面用，
// 英文界面直接展示原始 type/rel，避免中英混排）
const TYPE_ZH: Record<string, string> = {
  metric: '指标',
  dataset: '数据集',
  dimension: '维度',
  domain: '业务域',
  entity: '知识实体',
  usermemory: '记忆',
};
const REL_ZH: Record<string, string> = {
  SURFACE_METRIC: '沉淀为指标',
  HAS: '包含',
  BELONGS_TO_DOMAIN: '属于域',
  MENTIONS: '提及',
  SUPPORTS: '支撑',
  DERIVED_FROM: '派生自',
};

const CELL = 19;
const GN = 9;
const GM = 7;
const OX = 470;
const TIER_OY: Record<number, number> = { 1: 490, 2: 310, 3: 130 };
// 层标题走 i18n：中文界面全中文、英文界面全英文，不再中英双行混排。
const TIER_META = [
  { t: 1, title: 'graphCenter.topicTierMetadata', sub: 'graphCenter.topicTierMetadataSub' },
  { t: 2, title: 'graphCenter.topicTierKnowledge', sub: 'graphCenter.topicTierKnowledgeSub' },
  { t: 3, title: 'graphCenter.topicTierUser', sub: 'graphCenter.topicTierUserSub' },
];

function iso(px: number, py: number, oy: number): Point {
  return { x: (px - py) * CELL, y: oy + (px + py) * CELL * 0.5 };
}

/** iso() 的逆变换：屏幕局部坐标 → 网格坐标（相对该层原点 oy）。 */
function gridOf(x: number, y: number, oy: number): Point {
  const a = x / CELL;
  const b = (y - oy) / (CELL * 0.5);
  return { x: (a + b) / 2, y: (b - a) / 2 };
}

/** 拖拽时平面跟随节点伸缩：以基础网格（GN×GM）为最小范围，向外整格扩展
 *  包住本层所有节点；每侧最多扩 PLANE_MAX_EXTRA 格（平面最大宽度的硬上限），
 *  拖到上限即被夹住，节点因此永远留在自己所属层的方形画布内。 */
const PLANE_PAD = 0.7;
const PLANE_MAX_EXTRA = 4;

const clampNum = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

interface PlaneRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}
const BASE_RECT: PlaneRect = { x0: 0, y0: 0, x1: GN, y1: GM };

function shade(hex: string, f: number): string {
  const n = parseInt(hex.slice(1), 16);
  const ch = (c: number) => {
    const t = f > 0 ? 255 : 0;
    return Math.round(c + (t - c) * Math.abs(f));
  };
  return `rgb(${ch((n >> 16) & 255)},${ch((n >> 8) & 255)},${ch(n & 255)})`;
}

// ── geometry ──────────────────────────────────────────────────────────
function convexHull(pts: Point[]): Point[] {
  if (pts.length < 3) return pts;
  const p = [...pts].sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o: Point, a: Point, b: Point) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lo: Point[] = [];
  for (const pt of p) {
    while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], pt) <= 0) lo.pop();
    lo.push(pt);
  }
  const hi: Point[] = [];
  for (const pt of [...p].reverse()) {
    while (hi.length >= 2 && cross(hi[hi.length - 2], hi[hi.length - 1], pt) <= 0) hi.pop();
    hi.push(pt);
  }
  return [...lo.slice(0, -1), ...hi.slice(0, -1)];
}

function blobPath(h: Point[], pad: number): string {
  if (h.length < 3) return '';
  const cx = h.reduce((a, p) => a + p.x, 0) / h.length;
  const cy = h.reduce((a, p) => a + p.y, 0) / h.length;
  const ex = h.map((p) => {
    const dx = p.x - cx,
      dy = p.y - cy;
    const d = Math.hypot(dx, dy) || 1;
    return { x: p.x + (dx / d) * pad, y: p.y + (dy / d) * pad };
  });
  const mid = (a: Point, b: Point) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  let d = `M ${mid(ex[0], ex[1]).x},${mid(ex[0], ex[1]).y}`;
  for (let i = 1; i <= ex.length; i++) {
    const v = ex[i % ex.length];
    const m = mid(v, ex[(i + 1) % ex.length]);
    d += ` Q ${v.x},${v.y} ${m.x},${m.y}`;
  }
  return d + ' Z';
}

// ── data build ────────────────────────────────────────────────────────
interface LaidMember extends TopicMember {
  tier: 1 | 2 | 3;
  sx: number;
  sy: number;
  deg: number;
}

function tierOf(m: TopicMember): 1 | 2 | 3 {
  return m.layer === 'MG' ? 1 : m.layer === 'UG' ? 3 : 2;
}

function buildTiers(topic: Topic): { members: LaidMember[]; byKey: Map<string, LaidMember> } {
  const degMap = new Map<string, number>();
  topic.edges.forEach((e) => {
    degMap.set(e.s, (degMap.get(e.s) || 0) + 1);
    degMap.set(e.t, (degMap.get(e.t) || 0) + 1);
  });
  const byKey = new Map<string, LaidMember>();
  const laid: LaidMember[] = [];
  ([1, 2, 3] as const).forEach((tier) => {
    const members = topic.members.filter((m) => tierOf(m) === tier);
    const ids = members.map((m) => m.key);
    const edges = topic.edges
      .filter((e) => ids.includes(e.s) && ids.includes(e.t))
      .map((e) => ({ source: e.s, target: e.t }));
    const local = forceCluster(ids, edges, 0, 0, null, {}, {});
    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    ids.forEach((id) => {
      const p = local[id];
      if (!p) return;
      minX = Math.min(minX, p.x);
      maxX = Math.max(maxX, p.x);
      minY = Math.min(minY, p.y);
      maxY = Math.max(maxY, p.y);
    });
    // 两轴分别归一：px 落在 [0.7, GN-0.7]、py 落在 [0.7, GM-0.7] ——
    // 旧版两轴都用 GN(=9) 归一，py 超出 GM(=7) 网格，节点会画到平面外。
    const nx = (v: number, a: number, b: number, max: number) =>
      b - a < 1e-6 ? max / 2 : 0.7 + ((v - a) / (b - a)) * (max - 1.4);
    members.forEach((m) => {
      const p = local[m.key] || { x: 0, y: 0 };
      const s = iso(nx(p.x, minX, maxX, GN), nx(p.y, minY, maxY, GM), TIER_OY[tier]);
      const lm: LaidMember = { ...m, tier, sx: s.x, sy: s.y, deg: degMap.get(m.key) || 0 };
      laid.push(lm);
      byKey.set(m.key, lm);
    });
  });
  return { members: laid, byKey };
}

// ── svg atoms ─────────────────────────────────────────────────────────
function GridPlane({ oy, rect = BASE_RECT }: { oy: number; rect?: PlaneRect }) {
  const lines: ReactNode[] = [];
  for (let i = rect.x0; i <= rect.x1; i++) {
    const a = iso(i, rect.y0, oy);
    const b = iso(i, rect.y1, oy);
    lines.push(
      <line key={`v${i}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={HAIR} strokeWidth={0.7} />,
    );
  }
  for (let j = rect.y0; j <= rect.y1; j++) {
    const a = iso(rect.x0, j, oy);
    const b = iso(rect.x1, j, oy);
    lines.push(
      <line key={`h${j}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={HAIR} strokeWidth={0.7} />,
    );
  }
  const c0 = iso(rect.x0, rect.y0, oy),
    c1 = iso(rect.x1, rect.y0, oy),
    c2 = iso(rect.x1, rect.y1, oy),
    c3 = iso(rect.x0, rect.y1, oy);
  return (
    <g>
      {lines}
      <polygon
        points={`${c0.x},${c0.y} ${c1.x},${c1.y} ${c2.x},${c2.y} ${c3.x},${c3.y}`}
        fill="rgba(148,163,255,0.03)"
        stroke={HAIR_STRONG}
        strokeWidth={1}
        filter="url(#planeGlow)"
      />
    </g>
  );
}

function Beam({
  x1,
  y1,
  x2,
  y2,
  kind,
  hot,
}: {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  kind: 'call' | 'distill';
  hot: boolean;
}) {
  const my = (y1 + y2) / 2;
  const w1 = kind === 'call' ? 2.5 : 2;
  const w2 = kind === 'call' ? 20 : 14;
  return (
    <path
      d={`M ${x1 - w1},${y1} C ${x1 - w1},${my} ${x2 - w2},${my} ${x2 - w2},${y2}
          L ${x2 + w2},${y2} C ${x2 + w2},${my} ${x1 + w1},${my} ${x1 + w1},${y1} Z`}
      fill={`url(#beam-${kind})`}
      opacity={hot ? 1 : 0.75}
      filter={hot ? 'url(#glowS)' : undefined}
    />
  );
}

function FlatNode({
  x,
  y,
  r,
  color,
  hot,
}: {
  x: number;
  y: number;
  r: number;
  color: string;
  hot?: boolean;
}) {
  // 平铺在 iso 平面上的菱形（2:1 透视，与层平面网格同向）——
  // 替代旧 3D 立方体：无暗色侧面遮挡，小尺寸下更亮更可读。
  const pts = `${x},${y - r * 0.5} ${x + r},${y} ${x},${y + r * 0.5} ${x - r},${y}`;
  return (
    <g filter="url(#glowS)">
      <polygon
        points={pts}
        fill={shade(color, hot ? 0.6 : 0.3)}
        stroke={hot ? '#fff' : shade(color, 0.8)}
        strokeWidth={hot ? 1.2 : 0.8}
      />
    </g>
  );
}

// ── main component ────────────────────────────────────────────────────
interface TopicStudioProps {
  topics: Topic[];
  topic: Topic;
  color: string;
  colorOf: (id: string) => string;
  onSelectTopic: (id: string) => void;
  onNodeClick?: (key: string) => void;
  onExit: () => void;
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 3.5;
const ZOOM_DEFAULT = 1.15;
const CENTER: Point = { x: 640, y: 325 };
/** 舞台 SVG 的 viewBox（meet 等比）—— 拖拽位移换算与 viewBox 共用一份常量。 */
const VB = { x: 262, y: -20, w: 750, h: 690 };

export default function TopicView({
  topics,
  topic,
  color,
  colorOf,
  onSelectTopic,
  onNodeClick,
  onExit,
}: TopicStudioProps) {
  const { t, i18n } = useTranslation();
  const isZh = (i18n.language || '').toLowerCase().startsWith('zh');
  const [selected, setSelected] = useState('');
  const [zoom, setZoom] = useState(ZOOM_DEFAULT);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [hoverKey, setHoverKey] = useState('');
  const [hoverPos, setHoverPos] = useState<Point>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  /** 用户拖过的节点坐标（覆盖布局位）；布局静态，随时可拖。 */
  const [userPos, setUserPos] = useState<Record<string, Point>>({});
  const stageRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const dragRef = useRef({ on: false, x: 0, y: 0, moved: false });
  /** 节点拖拽态：按住节点时接管指针，不让舞台平移抢。 */
  const nodeDragRef = useRef<{
    key: string;
    px: number;
    py: number;
    x: number;
    y: number;
    moved: boolean;
  } | null>(null);

  const { members: baseMembers } = useMemo(() => buildTiers(topic), [topic]);
  // 生效坐标：拖过的节点用拖拽位，其余用布局位；边/区域/图例全从
  // members/byKey 派生，在数据源头套一层，拖动时整张图（含区域轮廓、引线）一致跟随。
  const members = useMemo(
    () =>
      baseMembers.map((m) => {
        const p = userPos[m.key];
        return p ? { ...m, sx: p.x, sy: p.y } : m;
      }),
    [baseMembers, userPos],
  );
  const byKey = useMemo(() => new Map(members.map((m) => [m.key, m])), [members]);
  // 换主题 = 换布局基准，清掉旧的拖拽位
  useEffect(() => {
    setUserPos({});
    nodeDragRef.current = null;
  }, [topic.id]);
  const sel = selected ? byKey.get(selected) : null;
  const hover = hoverKey ? byKey.get(hoverKey) || null : null;
  const selEdges = sel ? topic.edges.filter((e) => e.s === sel.key || e.t === sel.key) : [];
  // 语言感知的语义文案：中文用译名表，英文直接用原始 type/rel
  const typeLabel = (ty: string) => (isZh ? TYPE_ZH[ty] || ty : ty);
  const roleLabel = (m: LaidMember) =>
    m.tier === 1
      ? t('graphCenter.legend.mg')
      : m.tier === 2
        ? `${t('graphCenter.legend.kg')} · ${m.layer}`
        : t('graphCenter.legend.ug');
  const relText = (e: { kind: string; rel: string; sim?: number }) =>
    e.kind === 'semantic'
      ? t('graphCenter.topicSemSim', { sim: (e.sim || 0).toFixed(2) })
      : isZh
        ? REL_ZH[e.rel] || e.rel
        : e.rel.replace(/_/g, ' ').toLowerCase();

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z * (e.deltaY < 0 ? 1.12 : 0.9))));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    dragRef.current = { on: true, x: e.clientX, y: e.clientY, moved: false };
    setDragging(true);
  };
  /** client px → 节点局部坐标的位移系数：先除 viewBox→屏幕的 meet 等比，再除内部 zoom。 */
  const dragScale = () => {
    const el = svgRef.current;
    if (!el) return 1 / zoom;
    const r = el.getBoundingClientRect();
    const s = Math.min(r.width / VB.w, r.height / VB.h);
    return s > 0 ? 1 / (s * zoom) : 1 / zoom;
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const r = stageRef.current?.getBoundingClientRect();
    if (r) setHoverPos({ x: e.clientX - r.left, y: e.clientY - r.top });
    const nd = nodeDragRef.current;
    if (nd) {
      const dx = e.clientX - nd.px,
        dy = e.clientY - nd.py;
      if (Math.abs(dx) + Math.abs(dy) > 3) nd.moved = true;
      if (nd.moved) {
        const k = dragScale();
        // 夹在所属层平面的最大范围内：平面会跟随节点向外伸展，但每侧最多
        // 扩 PLANE_MAX_EXTRA 格，拖到边界即停，节点不会离开方形画布。
        const lm = byKey.get(nd.key);
        let nx = nd.x + dx * k;
        let ny = nd.y + dy * k;
        if (lm) {
          const oy = TIER_OY[lm.tier];
          const g = gridOf(nx, ny, oy);
          const p = iso(
            clampNum(g.x, -PLANE_MAX_EXTRA + PLANE_PAD, GN + PLANE_MAX_EXTRA - PLANE_PAD),
            clampNum(g.y, -PLANE_MAX_EXTRA + PLANE_PAD, GM + PLANE_MAX_EXTRA - PLANE_PAD),
            oy,
          );
          nx = p.x;
          ny = p.y;
        }
        setUserPos((prev) => ({ ...prev, [nd.key]: { x: nx, y: ny } }));
      }
      return;
    }
    if (!dragRef.current.on) return;
    const dx = e.clientX - dragRef.current.x,
      dy = e.clientY - dragRef.current.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) dragRef.current.moved = true;
    const f = r ? 1240 / r.width : 1;
    setPan((p) => ({ x: p.x + dx * f, y: p.y + dy * f }));
    dragRef.current.x = e.clientX;
    dragRef.current.y = e.clientY;
  };
  const endDrag = () => {
    dragRef.current.on = false;
    setDragging(false);
    // 延到 click 之后再清，拖拽后的 click 才能被正确抑制
    window.setTimeout(() => {
      nodeDragRef.current = null;
    }, 0);
  };

  const xform = `translate(${CENTER.x + pan.x},${CENTER.y + pan.y}) scale(${zoom}) translate(${-CENTER.x},${-CENTER.y})`;

  // ── 挖掘画像（左上卡）：三层图谱统计 + 证据强度 + 覆盖域 ──────────────
  // 旧版“主题地图”散点（x=跨层熵、y=规模）无坐标轴注释、气泡互叠，
  // 用户无法理解；改为直接呈现挖掘产物：元数据、知识、用户图层的节点计数
  //（与中央三层平面一一对应）、结构/语义证据量与相似度、覆盖业务域。
  const funnel = useMemo(() => {
    const c: Record<1 | 2 | 3, number> = { 1: 0, 2: 0, 3: 0 };
    baseMembers.forEach((m) => {
      c[m.tier] += 1;
    });
    const max = Math.max(1, c[1], c[2], c[3]);
    return [
      {
        key: 'metadata',
        label: t('graphCenter.legend.mg'),
        count: c[1],
        color: GRAPH_COLOR.MG,
      },
      {
        key: 'knowledge',
        label: t('graphCenter.legend.kg'),
        count: c[2],
        color: GRAPH_COLOR.KG,
      },
      { key: 'distill', label: t('graphCenter.legend.ug'), count: c[3], color: DISTILL },
    ].map((r) => ({ ...r, pct: r.count ? Math.max(6, (r.count / max) * 100) : 0 }));
  }, [baseMembers, t]);

  const evidence = useMemo(() => {
    let hard = 0;
    let sem = 0;
    let simSum = 0;
    topic.edges.forEach((e) => {
      if (e.kind === 'semantic') {
        sem += 1;
        simSum += e.sim || 0;
      } else {
        hard += 1;
      }
    });
    return { hard, sem, avgSim: sem ? simSum / sem : 0 };
  }, [topic.edges]);

  const domains = useMemo(() => {
    const cnt = new Map<string, number>();
    topic.members.forEach((m) => {
      if (m.domain) cnt.set(m.domain, (cnt.get(m.domain) || 0) + 1);
    });
    return [...cnt.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  }, [topic.members]);

  // ── 主题洞察（右栏默认态）：核心资产 Top5 + 沉淀成果清单 ────────────
  const hubs = useMemo(
    () =>
      members
        .filter((m) => m.tier !== 3)
        .sort((a, b) => b.deg - a.deg)
        .slice(0, 5),
    [members],
  );
  const distilled = useMemo(() => members.filter((m) => m.tier === 3).slice(0, 6), [members]);

  // tier-2 regions per source graph
  const regions = useMemo(() => {
    const out: { layer: string; pts: Point[]; keys: string[] }[] = [];
    (['MG', 'KG', 'UG'] as const).forEach((L) => {
      const ms = members.filter((m) => m.tier === 2 && m.layer === L);
      if (!ms.length) return;
      out.push({
        layer: L,
        pts: ms.map((m) => ({ x: m.sx, y: m.sy })),
        keys: ms.map((m) => m.key),
      });
    });
    return out;
  }, [members]);

  // 平面随拖拽伸缩：整格包住本层所有节点（含拖拽位），基础网格为最小、
  // 每侧 PLANE_MAX_EXTRA 格为最大，确保节点始终落在方形画布内。
  const planes = useMemo(() => {
    const out: Record<1 | 2 | 3, PlaneRect> = {
      1: { ...BASE_RECT },
      2: { ...BASE_RECT },
      3: { ...BASE_RECT },
    };
    members.forEach((m) => {
      const g = gridOf(m.sx, m.sy, TIER_OY[m.tier]);
      const r = out[m.tier];
      r.x0 = Math.max(-PLANE_MAX_EXTRA, Math.min(r.x0, Math.floor(g.x - PLANE_PAD)));
      r.y0 = Math.max(-PLANE_MAX_EXTRA, Math.min(r.y0, Math.floor(g.y - PLANE_PAD)));
      r.x1 = Math.min(GN + PLANE_MAX_EXTRA, Math.max(r.x1, Math.ceil(g.x + PLANE_PAD)));
      r.y1 = Math.min(GM + PLANE_MAX_EXTRA, Math.max(r.y1, Math.ceil(g.y + PLANE_PAD)));
    });
    return out;
  }, [members]);

  const edgeHot = (e: { s: string; t: string }) =>
    (sel && (e.s === sel.key || e.t === sel.key)) || hoverKey === e.s || hoverKey === e.t;

  // 右侧图例：锚点全部取真实几何（最靠右的沉淀节点 / 跨层光束中点 /
  // 来源图区域轮廓最右点），引线保证落在对应元素上；数据缺席的图例隐藏。
  const legends = useMemo(() => {
    const out: { key: string; ax: number; ay: number; ty: number; label: string }[] = [];
    const t3 = members.filter((m) => m.tier === 3);
    if (t3.length) {
      const m = t3.reduce((a, b) => (b.sx > a.sx ? b : a));
      out.push({
        key: 'distill',
        ax: m.sx + 10,
        ay: m.sy - 6,
        ty: 0,
        label: t('graphCenter.topicLegendDistill'),
      });
    }
    let beam: Point | null = null;
    topic.edges.forEach((e) => {
      const a = byKey.get(e.s);
      const b = byKey.get(e.t);
      if (!a || !b || a.tier === b.tier) return;
      const mid = { x: (a.sx + b.sx) / 2, y: (a.sy + b.sy) / 2 };
      if (!beam || mid.x > beam.x) beam = mid;
    });
    if (beam) {
      const bm = beam as Point;
      out.push({
        key: 'beam',
        ax: bm.x + 8,
        ay: bm.y,
        ty: 0,
        label: t('graphCenter.topicLegendBeam'),
      });
    }
    let region: Point | null = null;
    regions.forEach((rg) =>
      rg.pts.forEach((p) => {
        if (!region || p.x > region.x) region = p;
      }),
    );
    if (region) {
      const rp = region as Point;
      out.push({
        key: 'region',
        ax: rp.x + 28,
        ay: rp.y,
        ty: 0,
        label: t('graphCenter.topicLegendRegion'),
      });
    }
    // 文字纵向防叠：引线起点保持在锚点，文字端逐条错开
    out.sort((a, b) => a.ay - b.ay);
    let prev = -Infinity;
    out.forEach((lg) => {
      lg.ty = Math.max(lg.ay - 8, prev + 20);
      prev = lg.ty;
    });
    return out;
  }, [members, topic.edges, byKey, regions, t]);

  return (
    <div className={styles.studio}>
      {/* ── left rail ── */}
      <aside className={styles.rail}>
        <div className={styles.railTitle}>{t('graphCenter.topicMapTitle')}</div>
        <div className={styles.railSub}>{t('graphCenter.topicMapSub')}</div>
        {topic.summary && <div className={styles.profileSummary}>{topic.summary}</div>}
        <div className={styles.profileFunnel}>
          {funnel.map((row) => (
            <div key={row.key} className={styles.funnelRow}>
              <span className={styles.funnelLabel}>{row.label}</span>
              <span className={styles.funnelTrack}>
                <span
                  className={styles.funnelBar}
                  style={{ width: `${row.pct}%`, background: row.color }}
                />
              </span>
              <span className={styles.funnelNum} style={{ color: row.color }}>
                {row.count}
              </span>
            </div>
          ))}
        </div>
        <div className={styles.profileStats}>
          <span>
            {t('graphCenter.topicEvidenceLine', { hard: evidence.hard, sem: evidence.sem })}
          </span>
          {evidence.sem > 0 && (
            <span>{t('graphCenter.topicSimLine', { sim: evidence.avgSim.toFixed(2) })}</span>
          )}
        </div>
        {domains.length > 0 && (
          <div className={styles.profileChips}>
            {domains.map(([d, n]) => (
              <span key={d} className={styles.profileChip}>
                {d} · {n}
              </span>
            ))}
          </div>
        )}
        <div className={styles.railList}>
          {topics.map((tp) => {
            const c = colorOf(tp.id);
            const on = tp.id === topic.id;
            return (
              <div
                key={tp.id}
                className={`${styles.railItem} ${on ? styles.railItemOn : ''}`}
                onClick={() => onSelectTopic(tp.id)}
              >
                <span
                  className={styles.railDot}
                  style={{ background: `linear-gradient(135deg, ${c}, ${DISTILL})` }}
                />
                <div>
                  <div className={styles.railItemTitle} style={on ? { color: c } : undefined}>
                    {tp.title}
                  </div>
                  <div className={styles.railItemMeta}>
                    {(['MG', 'KG', 'UG'] as const)
                      .map((L) => (tp.layer_counts[L] ? `${L}·${tp.layer_counts[L]} ` : ''))
                      .join('')}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <div className={styles.railFoot}>
          <Button size="small" onClick={onExit}>
            {t('graphCenter.topicBackGlobal')}
          </Button>
        </div>
      </aside>

      {/* ── center stage ── */}
      <div
        className={styles.stage}
        ref={stageRef}
        style={{
          cursor: dragging ? 'grabbing' : 'grab',
          background: `radial-gradient(1200px 700px at 50% 30%, ${BG_TOP}, ${BG_BOT})`,
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
      >
        <svg
          ref={svgRef}
          viewBox={`${VB.x} ${VB.y} ${VB.w} ${VB.h}`}
          className={styles.stageSvg}
          preserveAspectRatio="xMidYMid meet"
        >
          <defs>
            <filter id="glowS" x="-80%" y="-80%" width="260%" height="260%">
              <feGaussianBlur stdDeviation="2.2" result="b" />
              <feMerge>
                <feMergeNode in="b" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <filter id="glowL" x="-80%" y="-80%" width="260%" height="260%">
              <feGaussianBlur stdDeviation="7" result="b" />
              <feMerge>
                <feMergeNode in="b" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <filter id="planeGlow" x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="3" result="b" />
              <feMerge>
                <feMergeNode in="b" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <filter id="railGlow" x="-120%" y="-120%" width="340%" height="340%">
              <feGaussianBlur stdDeviation="4" />
            </filter>
            <linearGradient id="beam-call" x1="0" y1="1" x2="0" y2="0">
              <stop offset="0" stopColor={GRAPH_COLOR.MG} stopOpacity="0.06" />
              <stop offset="1" stopColor={GRAPH_COLOR.MG} stopOpacity="0.5" />
            </linearGradient>
            <linearGradient id="beam-distill" x1="0" y1="1" x2="0" y2="0">
              <stop offset="0" stopColor={GRAPH_COLOR.MG} stopOpacity="0.06" />
              <stop offset="1" stopColor={DISTILL} stopOpacity="0.55" />
            </linearGradient>
          </defs>

          <g transform={xform}>
            {/* tier titles */}
            {TIER_META.map((tm) => (
              <g key={tm.t}>
                <text
                  x={OX - 160}
                  y={TIER_OY[tm.t] - 6}
                  fontSize={16}
                  fontWeight={700}
                  fill={tm.t === 3 ? DISTILL : TXT}
                  letterSpacing={2}
                  fontFamily={MONO}
                  filter="url(#glowS)"
                >
                  {t(tm.title)}
                </text>
                <text
                  x={OX - 160}
                  y={TIER_OY[tm.t] + 10}
                  fontSize={11}
                  fill={TXT_DIM}
                  fontFamily={MONO}
                >
                  {t(tm.sub)}
                </text>
              </g>
            ))}

            {/* planes（随各层节点实际范围伸缩，上限见 PLANE_MAX_EXTRA） */}
            {([1, 2, 3] as const).map((tier) => (
              <g key={tier} transform={`translate(${OX},0)`}>
                <GridPlane oy={TIER_OY[tier]} rect={planes[tier]} />
              </g>
            ))}

            {/* tier-2 glowing regions per source graph */}
            <g transform={`translate(${OX},0)`}>
              {regions.map((rg) => {
                const c = GRAPH_COLOR[rg.layer];
                const hull = convexHull(rg.pts);
                // pad 收到 20：节点已限制在平面内，光晕也尽量不吐出平面边缘
                const path = blobPath(hull, 20);
                const cx = rg.pts.reduce((a, p) => a + p.x, 0) / rg.pts.length;
                const cy = rg.pts.reduce((a, p) => a + p.y, 0) / rg.pts.length;
                return (
                  <g key={rg.layer}>
                    {path ? (
                      <path
                        d={path}
                        fill={c}
                        fillOpacity={0.07}
                        stroke={c}
                        strokeOpacity={0.55}
                        strokeWidth={1.1}
                        filter="url(#glowL)"
                      />
                    ) : (
                      <ellipse
                        cx={rg.pts[0].x}
                        cy={rg.pts[0].y}
                        rx={30}
                        ry={18}
                        fill={c}
                        fillOpacity={0.07}
                        stroke={c}
                        strokeOpacity={0.55}
                        strokeWidth={1.1}
                        filter="url(#glowL)"
                      />
                    )}
                    <text
                      x={cx}
                      y={cy - 34}
                      textAnchor="middle"
                      fontSize={11}
                      fontWeight={700}
                      fill={c}
                      letterSpacing={2}
                      fontFamily={MONO}
                      filter="url(#glowS)"
                    >
                      {rg.layer} · {rg.keys.length}
                    </text>
                  </g>
                );
              })}
            </g>

            {/* edges */}
            <g transform={`translate(${OX},0)`}>
              {topic.edges.map((e, i) => {
                const a = byKey.get(e.s);
                const b = byKey.get(e.t);
                if (!a || !b) return null;
                const hot = edgeHot(e);
                // cross-tier beams
                if (a.tier !== b.tier) {
                  const lower = a.tier > b.tier ? a : b;
                  const upper = lower === a ? b : a;
                  const kind = upper.tier === 3 ? 'distill' : 'call';
                  if (e.kind === 'semantic' && !hot) {
                    return (
                      <path
                        key={i}
                        d={`M ${lower.sx},${lower.sy - 4} C ${lower.sx},${(lower.sy + upper.sy) / 2} ${upper.sx},${(lower.sy + upper.sy) / 2} ${upper.sx},${upper.sy - 2}`}
                        fill="none"
                        stroke={HAIR_STRONG}
                        strokeOpacity={0.25 + 0.3 * (e.sim || 0.7)}
                        strokeWidth={0.7}
                      />
                    );
                  }
                  return (
                    <Beam
                      key={i}
                      x1={lower.sx}
                      y1={lower.sy - 4}
                      x2={upper.sx}
                      y2={upper.sy - 2}
                      kind={kind}
                      hot={hot}
                    />
                  );
                }
                // same tier
                if (a.tier === 2) {
                  const sameRegion = a.layer === b.layer;
                  const c = sameRegion ? GRAPH_COLOR[a.layer] : '#8ea2ff';
                  return (
                    <line
                      key={i}
                      x1={a.sx}
                      y1={a.sy - 4}
                      x2={b.sx}
                      y2={b.sy - 4}
                      stroke={c}
                      strokeOpacity={hot ? 0.95 : sameRegion ? 0.4 : 0.55}
                      strokeWidth={hot ? 1.4 : sameRegion ? 0.8 : 1}
                      filter={hot ? 'url(#glowS)' : undefined}
                    />
                  );
                }
                return (
                  <line
                    key={i}
                    x1={a.sx}
                    y1={a.sy - 3}
                    x2={b.sx}
                    y2={b.sy - 3}
                    stroke={GRAPH_COLOR.MG}
                    strokeOpacity={hot ? 0.9 : 0.3}
                    strokeWidth={hot ? 1.2 : 0.7}
                  />
                );
              })}
            </g>

            {/* nodes */}
            <g transform={`translate(${OX},0)`}>
              {members.map((m) => {
                const c = m.tier === 3 ? DISTILL : GRAPH_COLOR[m.layer];
                const isSel = sel?.key === m.key;
                const isHover = hoverKey === m.key;
                const topDeg =
                  m.tier === 2 &&
                  [...members]
                    .filter((x) => x.tier === 2)
                    .sort((a, b) => b.deg - a.deg)
                    .slice(0, 4)
                    .some((t) => t.key === m.key);
                return (
                  <g
                    key={m.key}
                    onMouseEnter={() => setHoverKey(m.key)}
                    onMouseLeave={() => setHoverKey('')}
                    onPointerDown={(e) => {
                      if (e.button !== 0) return;
                      e.stopPropagation(); // 节点拖拽优先，不触发舞台平移
                      nodeDragRef.current = {
                        key: m.key,
                        px: e.clientX,
                        py: e.clientY,
                        x: m.sx,
                        y: m.sy,
                        moved: false,
                      };
                      setDragging(true);
                    }}
                    onClick={() => {
                      if (!dragRef.current.moved && !nodeDragRef.current?.moved) setSelected(m.key);
                    }}
                    style={{ cursor: 'grab' }}
                  >
                    {/* halo */}
                    <circle
                      cx={m.sx}
                      cy={m.sy - 4}
                      r={(m.tier === 1 ? 8 : 12) + (isHover ? 4 : 0)}
                      fill={c}
                      opacity={isHover || isSel ? 0.4 : 0.16}
                      filter="url(#glowL)"
                    />
                    {m.tier === 1 && (
                      <circle
                        cx={m.sx}
                        cy={m.sy - 4}
                        r={isHover ? 4.5 : 3.2}
                        fill={c}
                        filter="url(#glowS)"
                      />
                    )}
                    {m.tier === 2 && (
                      <FlatNode
                        x={m.sx}
                        y={m.sy - 4}
                        r={5 + Math.min(6, m.deg * 0.7) + (isHover ? 1.5 : 0)}
                        color={c}
                        hot={isHover}
                      />
                    )}
                    {m.tier === 3 && (
                      <g filter="url(#glowS)">
                        <polygon
                          points={`${m.sx},${m.sy - 14} ${m.sx + 7},${m.sy - 5} ${m.sx},${m.sy + 4} ${m.sx - 7},${m.sy - 5}`}
                          fill={shade(c, isHover ? 0.5 : 0.2)}
                          stroke={shade(c, 0.8)}
                          strokeWidth={0.8}
                        />
                        <line
                          x1={m.sx - 11}
                          y1={m.sy - 5}
                          x2={m.sx + 11}
                          y2={m.sy - 5}
                          stroke={c}
                          strokeOpacity={0.7}
                          strokeWidth={0.6}
                        />
                        <line
                          x1={m.sx}
                          y1={m.sy - 18}
                          x2={m.sx}
                          y2={m.sy + 8}
                          stroke={c}
                          strokeOpacity={0.7}
                          strokeWidth={0.6}
                        />
                      </g>
                    )}
                    {isSel && (
                      <polygon
                        points={`${m.sx},${m.sy - 4 - 11} ${m.sx + 22},${m.sy - 4} ${m.sx},${m.sy - 4 + 11} ${m.sx - 22},${m.sy - 4}`}
                        fill="none"
                        stroke={color}
                        strokeWidth={1.1}
                        filter="url(#glowS)"
                      />
                    )}
                    {(topDeg || isHover) && (
                      <text
                        x={m.sx}
                        y={m.sy + 14}
                        textAnchor="middle"
                        fontSize={10.5}
                        fill={isHover ? '#fff' : TXT}
                        fontWeight={isHover ? 700 : 400}
                        fontFamily={MONO}
                      >
                        {m.label.length > 14 ? m.label.slice(0, 14) + '…' : m.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>

            {/* 右侧图例：引线从真实元素锚点出发，不再悬空在平面外 */}
            <g transform={`translate(${OX},0)`} fontFamily={MONO} fontSize={11} fill={TXT_DIM}>
              {legends.map((lg) => (
                <g key={lg.key}>
                  <circle cx={lg.ax} cy={lg.ay} r={1.6} fill={TXT_DIM} />
                  <line
                    x1={lg.ax}
                    y1={lg.ay}
                    x2={296}
                    y2={lg.ty - 4}
                    stroke={HAIR_STRONG}
                    strokeWidth={0.7}
                  />
                  <text x={302} y={lg.ty}>
                    {lg.label}
                  </text>
                </g>
              ))}
            </g>
          </g>
        </svg>

        {/* controls */}
        <div className={styles.stageControls} onPointerDown={(e) => e.stopPropagation()}>
          <button
            className={styles.ctlBtn}
            onClick={() => setZoom((z) => Math.max(ZOOM_MIN, +(z - 0.25).toFixed(2)))}
          >
            −
          </button>
          <input
            className={styles.zoomSlider}
            type="range"
            min={ZOOM_MIN}
            max={ZOOM_MAX}
            step={0.05}
            value={zoom}
            onChange={(e) => setZoom(parseFloat(e.target.value))}
          />
          <button
            className={styles.ctlBtn}
            onClick={() => setZoom((z) => Math.min(ZOOM_MAX, +(z + 0.25).toFixed(2)))}
          >
            ＋
          </button>
          <span className={styles.zoomVal}>{zoom.toFixed(2)}×</span>
          <button
            className={styles.ctlBtn}
            onClick={() => {
              setZoom(ZOOM_DEFAULT);
              setPan({ x: 0, y: 0 });
              setUserPos({});
            }}
          >
            {t('graphCenter.resetView')}
          </button>
        </div>

        {hover && (
          <div className={styles.tooltip} style={{ left: hoverPos.x + 14, top: hoverPos.y + 10 }}>
            <div className={styles.tooltipTitle}>{hover.label}</div>
            <div className={styles.tooltipMeta}>
              {roleLabel(hover)} · {typeLabel(hover.type)} ·{' '}
              {t('graphCenter.topicLinkCount', { num: hover.deg })}
            </div>
          </div>
        )}
      </div>

      {/* ── right rail ── */}
      <aside className={styles.detail}>
        {!sel ? (
          <>
            {/* 未选中节点：先给主题级洞察（核心资产 / 沉淀成果），行点击即选中
                对应节点；原来整栏只挂一句引导语，用户无从理解这个面板的价值。 */}
            <div className={styles.detailKicker}>{t('graphCenter.topicInsightKicker')}</div>
            <div className={styles.evidenceKicker}>{t('graphCenter.topicInsightHubs')}</div>
            <ul className={styles.evidenceList}>
              {hubs.map((m) => (
                <li key={m.key} onClick={() => setSelected(m.key)}>
                  <span className={styles.evidenceLayer} style={{ color: GRAPH_COLOR[m.layer] }}>
                    {m.layer}
                  </span>
                  <span className={styles.evidenceLabel}>{m.label}</span>
                  <span className={styles.evidenceRel}>
                    {t('graphCenter.topicLinkCount', { num: m.deg })}
                  </span>
                </li>
              ))}
              {hubs.length === 0 && (
                <li className={styles.detailSub}>{t('graphCenter.topicInsightNoHubs')}</li>
              )}
            </ul>
            <div className={styles.evidenceKicker}>{t('graphCenter.topicInsightDistill')}</div>
            {distilled.length ? (
              <ul className={styles.evidenceList}>
                {distilled.map((m) => (
                  <li key={m.key} onClick={() => setSelected(m.key)}>
                    <span className={styles.evidenceLayer} style={{ color: DISTILL }}>
                      {typeLabel(m.type)}
                    </span>
                    <span className={styles.evidenceLabel}>{m.label}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className={styles.insightGap}>{t('graphCenter.topicInsightNoDistill')}</div>
            )}
            <div className={styles.detailEmpty}>{t('graphCenter.topicDetailEmpty')}</div>
          </>
        ) : (
          <>
            <div className={styles.detailKicker}>{t('graphCenter.topicDetailKicker')}</div>
            <div className={styles.detailHead}>
              <span
                className={styles.detailLayer}
                style={{ color: sel.tier === 3 ? DISTILL : GRAPH_COLOR[sel.layer] }}
              >
                {roleLabel(sel)} · {typeLabel(sel.type)}
              </span>
            </div>
            <div className={styles.detailTitle}>{sel.label}</div>
            {sel.domain && (
              <div className={styles.detailSub}>
                {t('graphCenter.topicDomainLabel', { name: sel.domain })}
              </div>
            )}
            {sel.text ? (
              <div className={styles.detailText}>{sel.text}</div>
            ) : (
              <div className={styles.detailSub}>{t('graphCenter.topicNoText')}</div>
            )}
            <div className={styles.detailSub} style={{ marginTop: 8 }}>
              {t('graphCenter.topicKeyLine', { key: sel.key })}
            </div>
            <div className={styles.evidenceKicker}>
              {t('graphCenter.topicEvidence', { num: selEdges.length })}
            </div>
            <ul className={styles.evidenceList}>
              {selEdges.map((e, i) => {
                const otherKey = e.s === sel.key ? e.t : e.s;
                const other = byKey.get(otherKey);
                if (!other) return null;
                return (
                  <li key={i} onClick={() => setSelected(otherKey)}>
                    <span
                      className={styles.evidenceLayer}
                      style={{ color: other.tier === 3 ? DISTILL : GRAPH_COLOR[other.layer] }}
                    >
                      {other.layer}
                    </span>
                    <span className={styles.evidenceLabel}>{other.label}</span>
                    <span className={styles.evidenceRel}>{relText(e)}</span>
                  </li>
                );
              })}
              {selEdges.length === 0 && (
                <li className={styles.detailSub}>{t('graphCenter.topicNoEdge')}</li>
              )}
            </ul>
            <div className={styles.detailFoot}>
              <Button size="small" type="primary" ghost onClick={() => onNodeClick?.(sel.key)}>
                {t('graphCenter.topicOpenDetail')}
              </Button>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
