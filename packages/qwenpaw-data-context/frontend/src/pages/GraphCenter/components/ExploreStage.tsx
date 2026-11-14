/** 自主探索舞台：模型经 V3 接口自由 tool-calling 探索图谱，画布随工具结果
 *  增量展开，左侧时间线如实回放每一步（thinking / tool_call / tool_result）。
 *  无任何写死阶段——顺序、轮次、停止时机全由模型决定。 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeftOutlined,
  BulbOutlined,
  CheckCircleFilled,
  ExclamationCircleFilled,
  LoadingOutlined,
  ReloadOutlined,
  RobotOutlined,
  WarningOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { startExplore } from '@/services/cmgraph/explore';
import type { ExploreGraphPayloadEdge, ExploreGraphPayloadNode } from '@/services/cmgraph/explore';
import { forceCluster, type Point } from './forceLayout';
import { routeEdges, type NodeVis, type RoutedEdge } from './exploreEdgeRouting';
import MiniMarkdown from './MiniMarkdown';
import styles from './exploreStage.module.less';

const LABEL_COLOR: Record<string, string> = {
  Metric: '#6366f1',
  Formula: '#f59e0b',
  Dimension: '#10b981',
  TaxonomyValue: '#34d399',
  Dataset: '#2563eb',
  DatasetColumn: '#64748b',
  Domain: '#7c4dd6',
  DataSource: '#0ea5e9',
  Claim: '#db2777',
  Strategy: '#d97706',
  Entity: '#14b8a6',
  Event: '#e11d48',
  Task: '#8b5cf6',
  Step: '#a78bfa',
};
const DEFAULT_COLOR = '#94a3b8';

/** 展开波配色（原型 BR 调色板延伸）：同波边同色，路径一眼可分。 */
const WAVE_COLORS = [
  '#8b5cf6',
  '#10b981',
  '#f59e0b',
  '#0ea5e9',
  '#ef4444',
  '#ec4899',
  '#14b8a6',
  '#f97316',
];

const TOOL_LABEL: Record<string, string> = {
  describe_graph: '了解拓扑',
  find_anchors: '定位锚点',
  expand_subgraph: '沿边展开',
  enumerate_domain: '枚举业务域',
  graph_query: 'Cypher 查询',
};

const JSON_TOKEN =
  /("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

function tryParseJson(value: unknown): unknown | undefined {
  if (value == null) return undefined;
  if (typeof value === 'object') return value;
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const candidates = [trimmed, trimmed.replace(/\n?…（截断）\s*$/, '')];
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      /* try next */
    }
  }
  return undefined;
}

function highlightJson(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let match: RegExpExecArray | null;
  JSON_TOKEN.lastIndex = 0;
  while ((match = JSON_TOKEN.exec(text))) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const tok = match[0];
    let cls = styles.jsonNum;
    if (tok[0] === '"') cls = /:\s*$/.test(tok) ? styles.jsonKey : styles.jsonStr;
    else if (tok === 'true' || tok === 'false') cls = styles.jsonBool;
    else if (tok === 'null') cls = styles.jsonNull;
    nodes.push(
      <span key={i} className={cls}>
        {tok}
      </span>,
    );
    i += 1;
    last = match.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function JsonBlock({ value, compact = false }: { value: unknown; compact?: boolean }) {
  const parsed = tryParseJson(value);
  const pretty =
    parsed === undefined
      ? typeof value === 'string'
        ? value
        : String(value)
      : JSON.stringify(parsed, null, 2);
  return (
    <pre className={compact ? styles.jsonCompact : styles.jsonBlock}>
      {parsed === undefined ? pretty : highlightJson(pretty)}
    </pre>
  );
}

interface TraceEntry {
  key: string;
  round: number;
  kind: 'thinking' | 'call';
  text?: string;
  name?: string;
  args?: Record<string, unknown>;
  status?: 'running' | 'ok' | 'error';
  resultSummary?: string;
  /** 工具完整返回（截断保留），可展开查看模型实际看到了什么。 */
  raw?: string;
  graphCounts?: { nodes: number; edges: number };
}

interface CanvasNode extends ExploreGraphPayloadNode {
  order: number;
}

interface CanvasEdge extends ExploreGraphPayloadEdge {
  /** 第几次「展开波」——同波边同色，最新波带 dashflow 流转。 */
  wave: number;
}

/** 一次产生图载荷的工具调用 = 一个「推理波」= 画布上一个簇。 */
interface WaveMeta {
  wave: number;
  round: number;
  tool: string;
  kind: 'anchors' | 'expand';
  seeds: string[];
  nodeCount: number;
  edgeCount: number;
}

type Phase = 'connecting' | 'running' | 'final' | 'error';

/** 簇内节点超过该数 → 默认折叠成超节点，用户显式点击才展开。 */
const COLLAPSE_THRESHOLD = 6;

/** 折叠态预览节点数（种子+hub 优先），其余收进 +N 气泡。 */
const COLLAPSE_PREVIEW = 8;

/** 相邻推理波行之间的空隙；行高全部由内容决定，框/标题牌不会越行。 */
const ROW_GAP = 28;

/** 标签感知碰撞半径：节点圆 + 下方 ≤9 字标签的近似横向占位（半宽）。
 *  两枚相邻标签横向至少需要 2×56=112px；种子带「展开起点」标注再加余量。 */
const LABEL_R = 56;
const SEED_R_BONUS = 14;
/** 展开行的内容上下留白：上方给骑缝步骤牌 + 种子标注，下方给节点标签。 */
const ROW_PAD_TOP = 64;
const ROW_PAD_BOTTOM = 48;

/** 过宽压缩后的防叠分离：横向按 2.1:1 折算标签占位，只在 y 方向推开，
 *  行高随内容自然长高，绝不牺牲标签间距。 */
function separateLabels(ids: string[], p: Record<string, Point>, need: number) {
  for (let pass = 0; pass < 30; pass++) {
    let moved = false;
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = p[ids[i]];
        const b = p[ids[j]];
        const d = Math.hypot((b.x - a.x) / 2.1, b.y - a.y);
        if (d >= need) continue;
        const push = (need - d) / 2 + 1;
        const s = b.y >= a.y ? 1 : -1;
        a.y -= s * push;
        b.y += s * push;
        moved = true;
      }
    }
    if (!moved) break;
  }
}

/** 折叠预览行的弧形振幅：单排等高会让同源边（种子→各预览节点）近似共线
 *  叠成一束；沿正弦弧排布后 dy/dx 单调变化，每条边的出射角互不相同（与
 *  锚点波的扇形弧同一手法）；相邻槽位另加 ±8 交错保证标签错位。 */
const ARC_AMP = 30;

/** 画布上的推理簇盒子（纵向时间轴的一行）。 */
interface ClusterBox {
  wave: number;
  cx: number;
  cy: number;
  top: number;
  h: number;
  /** 折叠行 +N 超节点的画布位置（紧跟预览末位，而非画布右缘）。 */
  proxy?: Point;
}

export interface ExploreStageProps {
  query: string;
  onBack: () => void;
  /** Rendered when the SSE connection itself fails (backend offline etc.). */
  fallback?: React.ReactNode;
}

export default function ExploreStage({ query, onBack, fallback }: ExploreStageProps) {
  const { t, i18n } = useTranslation();
  const [phase, setPhase] = useState<Phase>('connecting');
  const [useFallback, setUseFallback] = useState(false);
  const [model, setModel] = useState('');
  const [trace, setTrace] = useState<TraceEntry[]>([]);
  const [nodes, setNodes] = useState<Map<string, CanvasNode>>(new Map());
  const [edges, setEdges] = useState<Map<string, CanvasEdge>>(new Map());
  const [budgetReason, setBudgetReason] = useState('');
  const [finalText, setFinalText] = useState('');
  const [finalOpen, setFinalOpen] = useState(false);
  const [finalMeta, setFinalMeta] = useState<{
    rounds: number;
    tool_calls: number;
    elapsed_ms: number;
  } | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [runId, setRunId] = useState(0);
  const [selected, setSelected] = useState<CanvasNode | null>(null);
  const [waves, setWaves] = useState<WaveMeta[]>([]);
  const [focusWave, setFocusWave] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  /** 用户拖过的节点坐标（覆盖布局位）—— 探索定格后才开放自由拖动。 */
  const [userPos, setUserPos] = useState<Record<string, Point>>({});

  const traceEndRef = useRef<HTMLDivElement | null>(null);
  /** 每波的局部（原点系）力导向坐标缓存 —— warm start 用局部系，
   *  行重排/缩放不会反馈污染下一轮求解。 */
  const waveLocalRef = useRef<Map<number, Record<string, Point>>>(new Map());
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [canvasSize, setCanvasSize] = useState({ w: 860, h: 560 });
  /** 节点首次出现的波次 —— 决定它属于哪个推理簇。 */
  const nodeWaveRef = useRef<Map<string, number>>(new Map());
  /** name → key，模型 seeds 有时传 name，解析成画布 key 才能高亮/连线。 */
  const nameToKeyRef = useRef<Map<string, string>>(new Map());
  /** tool_call 时记下 seeds，等 tool_result 归入同一波。 */
  const seedsByCallRef = useRef<Record<string, string[]>>({});

  // 画布视口：滚轮以指针为中心缩放，按住拖拽平移（与全局图谱同款手感）
  const [view, setView] = useState({ x: 0, y: 0, s: 1 });
  const [svgEl, setSvgEl] = useState<SVGSVGElement | null>(null);
  const dragRef = useRef<{ px: number; py: number; x: number; y: number; moved: boolean } | null>(
    null,
  );
  /** 节点拖拽态：按住节点时接管指针移动，不让画布平移抢。 */
  const nodeDragRef = useRef<{
    key: string;
    px: number;
    py: number;
    x: number;
    y: number;
    moved: boolean;
  } | null>(null);
  // 运行中自动滚到最新一波；用户手动滚动/拖拽后停止跟随
  const followRef = useRef(true);
  const progScrollRef = useRef(false);
  useEffect(() => {
    if (!svgEl) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const rect = svgEl.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const py = ev.clientY - rect.top;
      setView((v) => {
        const s = Math.min(3, Math.max(0.35, v.s * (ev.deltaY < 0 ? 1.15 : 1 / 1.15)));
        const k = s / v.s;
        return { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
      });
    };
    svgEl.addEventListener('wheel', onWheel, { passive: false });
    return () => svgEl.removeEventListener('wheel', onWheel);
  }, [svgEl]);
  // 在 svg 外松手也要清拖拽态，否则下一次点击会被误判为拖拽后抑制
  useEffect(() => {
    const up = () => {
      window.setTimeout(() => {
        dragRef.current = null;
        nodeDragRef.current = null;
      }, 0);
    };
    window.addEventListener('pointerup', up);
    return () => window.removeEventListener('pointerup', up);
  }, []);

  // 逐粒展现队列：批载荷先进 pending，定时器每 110ms 放 2 个节点上画布，
  // 复刻原型的「节点逐个 pop、边随后 draw-in」动态形式。
  const pendingRef = useRef<string[]>([]);
  const knownKeysRef = useRef<Set<string>>(new Set());
  const [revealed, setRevealed] = useState<Set<string>>(new Set());

  // 原型同款动态语汇：底部 narration 解说丸、种子 srcpulse 回闪、
  // 最新一波边的 dashflow 流转、query 节点脉冲。wave = 第几次「展开波」。
  const [narration, setNarration] = useState('');
  const [pulseKeys, setPulseKeys] = useState<Set<string>>(new Set());
  const [currentWave, setCurrentWave] = useState(0);
  const waveRef = useRef(0);
  const anchorKeysRef = useRef<string[]>([]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!pendingRef.current.length) return;
      const batch = pendingRef.current.splice(0, 2);
      setRevealed((prev) => {
        const next = new Set(prev);
        batch.forEach((k) => next.add(k));
        return next;
      });
    }, 110);
    return () => window.clearInterval(timer);
  }, []);

  // Canvas size tracking
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setCanvasSize({ w: Math.max(320, el.clientWidth), h: Math.max(300, el.clientHeight) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [useFallback]);

  // Auto-scroll the timeline
  useEffect(() => {
    traceEndRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [trace.length]);

  // Run the SSE session (re-runs when query or runId changes)
  useEffect(() => {
    setPhase('connecting');
    setUseFallback(false);
    setTrace([]);
    setNodes(new Map());
    setEdges(new Map());
    setFinalText('');
    setFinalOpen(false);
    setFinalMeta(null);
    setBudgetReason('');
    setErrorMsg('');
    setSelected(null);
    waveLocalRef.current = new Map();
    pendingRef.current = [];
    knownKeysRef.current = new Set();
    setRevealed(new Set());
    waveRef.current = 0;
    setCurrentWave(0);
    anchorKeysRef.current = [];
    setNarration('');
    setPulseKeys(new Set());
    setWaves([]);
    setFocusWave(null);
    setCollapsed(new Set());
    setUserPos({});
    nodeDragRef.current = null;
    setView({ x: 0, y: 0, s: 1 });
    followRef.current = true;
    nodeWaveRef.current = new Map();
    nameToKeyRef.current = new Map();
    seedsByCallRef.current = {};

    const mergeGraph = (
      graphNodes: ExploreGraphPayloadNode[],
      graphEdges: ExploreGraphPayloadEdge[],
      meta: { tool: string; round: number; callId: string },
    ) => {
      if (!graphNodes.length && !graphEdges.length) return;
      // 每次图载荷 = 一个推理波 = 画布上一个簇（锚点波只有点，展开波有点有边）
      waveRef.current += 1;
      const wave = waveRef.current;
      setCurrentWave(wave);
      const seeds = seedsByCallRef.current[meta.callId] || [];
      let newKeys: string[] = [];
      if (graphNodes.length) {
        // 新 key 用 ref 同步算（setNodes 的 updater 在渲染期才跑，不能在里面收集）
        newKeys = [];
        graphNodes.forEach((n) => {
          if (!n.key || knownKeysRef.current.has(n.key)) return;
          knownKeysRef.current.add(n.key);
          if (n.name) nameToKeyRef.current.set(n.name, n.key);
          nodeWaveRef.current.set(n.key, wave);
          newKeys.push(n.key);
        });
        pendingRef.current.push(...newKeys);
        // 锚点波（find_anchors 只有点没有边）：给每个出现的锚点都连一条淡虚线到「你的问题」，
        // 不再只连前 3 个，避免 12 个锚点却 3 条边、其余悬空的不一致观感。
        if (meta.tool === 'find_anchors') {
          anchorKeysRef.current.push(...newKeys.slice(0, 12 - anchorKeysRef.current.length));
        }
        setNodes((prev) => {
          const next = new Map(prev);
          graphNodes.forEach((n) => {
            if (!n.key) return;
            const existing = next.get(n.key);
            next.set(
              n.key,
              existing
                ? { ...existing, props: { ...(existing.props || {}), ...(n.props || {}) } }
                : { ...n, order: next.size },
            );
          });
          return next;
        });
      }
      setWaves((prev) => [
        ...prev,
        {
          wave,
          round: meta.round,
          tool: meta.tool,
          kind: meta.tool === 'find_anchors' ? 'anchors' : 'expand',
          seeds,
          nodeCount: graphNodes.length,
          edgeCount: graphEdges.length,
        },
      ]);
      // 节点太多的展开波默认折叠成超节点，避免不得不重叠的糊屏；
      // 锚点波永不自动折叠——找到的锚点必须直接展示（原型「锚点入图」）
      if (graphNodes.length > COLLAPSE_THRESHOLD && meta.tool !== 'find_anchors') {
        setCollapsed((prev) => new Set(prev).add(wave));
      }
      if (graphEdges.length) {
        // 每波 hub 保护：单节点在本波最多 8 条边，避免 Domain 星爆糊屏
        const degInWave: Record<string, number> = {};
        const kept: CanvasEdge[] = [];
        graphEdges.forEach((e) => {
          if (!e.from || !e.to) return;
          degInWave[e.from] = (degInWave[e.from] || 0) + 1;
          degInWave[e.to] = (degInWave[e.to] || 0) + 1;
          if (degInWave[e.from] > 8 || degInWave[e.to] > 8) return;
          kept.push({ ...e, wave });
        });
        setEdges((prev) => {
          const next = new Map(prev);
          kept.forEach((e) => next.set(`${e.from}|${e.rel}|${e.to}`, e));
          return next;
        });
      }
    };

    const cancel = startExplore(
      query,
      {
        onStart: (e) => {
          setModel(e.model);
          setPhase('running');
          setNarration('模型开始自主探索…');
        },
        onThinking: (e) => {
          setNarration(e.text.slice(0, 72));
          setTrace((prev) => [
            ...prev,
            {
              key: `th-${prev.length}-${e.round}`,
              round: e.round,
              kind: 'thinking',
              text: e.text,
            },
          ]);
        },
        onToolCall: (e) => {
          if (e.name === 'expand_subgraph') {
            const rawSeeds = Array.isArray(e.args?.seeds) ? (e.args.seeds as string[]) : [];
            // 种子常是「还没上画布的节点 key」（模型从锚点名推出来的），
            // 原样保留：画布显现到它们时自然配上高亮/起源线，不能提前过滤掉
            const nameResolved = rawSeeds
              .map((s) => nameToKeyRef.current.get(String(s)) || '')
              .filter((k) => k && !rawSeeds.includes(k));
            seedsByCallRef.current[e.id] = rawSeeds;
            if (rawSeeds.length) {
              // 常驻标注「展开起点」，直到下一次展开波替换
              setPulseKeys(new Set([...rawSeeds, ...nameResolved]));
            }
            setNarration(`从 ${rawSeeds.length} 个种子沿边展开…`);
          } else if (e.name === 'find_anchors') {
            setNarration(`定位锚点：「${String(e.args?.query || '')}」`);
          } else if (e.name === 'graph_query') {
            setNarration('Cypher 补查，验证关系链…');
          } else {
            setNarration('查看图谱拓扑…');
          }
          setTrace((prev) => [
            ...prev,
            {
              key: e.id,
              round: e.round,
              kind: 'call',
              name: e.name,
              args: e.args,
              status: 'running',
            },
          ]);
        },
        onToolResult: (e) => {
          let summary = '';
          try {
            const parsed = JSON.parse(e.result);
            if (parsed && typeof parsed === 'object') {
              if (Array.isArray(parsed.anchors)) summary = `命中 ${parsed.anchors.length} 个锚点`;
              else if (parsed.nodes && typeof parsed.nodes === 'object') {
                summary = `${Object.keys(parsed.nodes).length} 节点 / ${(parsed.edges || []).length} 边`;
              } else if (Array.isArray(parsed.rows)) summary = `${parsed.count} 行`;
              else if (parsed.schema || parsed.label_counts) {
                const lc = parsed.label_counts || {};
                summary = `schema 卡 + ${parsed.node_total || 0} 节点 / ${Object.keys(lc).length} 类 label`;
              }
            }
          } catch {
            /* raw string result */
          }
          if (!summary) summary = e.ok ? '完成' : '失败';
          setTrace((prev) =>
            prev.map((entry) =>
              entry.key === e.id
                ? {
                    ...entry,
                    status: e.ok ? 'ok' : 'error',
                    resultSummary: summary,
                    raw:
                      e.result.length > 6000 ? `${e.result.slice(0, 6000)}\n…（截断）` : e.result,
                    graphCounts: e.graph
                      ? { nodes: e.graph.nodes.length, edges: e.graph.edges.length }
                      : undefined,
                  }
                : entry,
            ),
          );
          if (e.ok && e.graph) {
            mergeGraph(e.graph.nodes, e.graph.edges, {
              tool: e.name,
              round: e.round,
              callId: e.id,
            });
            if (e.graph.edges.length) {
              setNarration(
                `展开 +${e.graph.nodes.length} 节点 +${e.graph.edges.length} 边，沿新路径流动`,
              );
            } else if (e.graph.nodes.length) {
              setNarration(`命中 ${e.graph.nodes.length} 个锚点，逐个上画布`);
            }
          } else if (!e.ok) {
            setNarration(`${e.name} 未达预期，模型自行调整路径`);
          }
        },
        onBudget: (e) => setBudgetReason(e.reason),
        onFinal: (e) => {
          setFinalText(e.text);
          setFinalMeta({ rounds: e.rounds, tool_calls: e.tool_calls, elapsed_ms: e.elapsed_ms });
          setPhase('final');
          setNarration(`✓ 探索完成：${e.rounds} 轮 · ${e.tool_calls} 次工具调用`);
        },
        onError: (message) => {
          setErrorMsg(message);
          setPhase('error');
        },
        onConnectError: (status, message) => {
          setErrorMsg(`HTTP ${status}${message ? `：${message}` : ''}`);
          setPhase('error');
          if (fallback) setUseFallback(true);
        },
      },
      { maxRounds: 8, lang: i18n.language },
    );
    return cancel;
    // fallback 是每次渲染都会重建的内联 JSX，不能进依赖数组，
    // 否则父组件任何状态变化都会重启 SSE 会话。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, runId]);

  // 展开/折叠某个推理簇。展开时把该波节点重新塞回逐粒队列，保持 pop 节奏。
  const toggleWave = (w: number) => {
    const wasCollapsed = collapsed.has(w);
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (wasCollapsed) next.delete(w);
      else next.add(w);
      return next;
    });
    if (wasCollapsed) {
      // 展开：已显现的（预览）节点保持可见，只把没显现的排进逐粒队列，
      // 不再整行清空重放——否则大波展开后先黑屏好几秒
      const keys = [...nodes.keys()].filter(
        (k) => (nodeWaveRef.current.get(k) ?? 0) === w && !revealed.has(k),
      );
      pendingRef.current.unshift(...keys);
    }
  };

  // Layout —— 推理顺序 = 空间顺序：每个「推理波」一个力导向簇，蛇形排在
  // 画布里；波内边参与簇内布局，跨波边只画参考线，避免模型跳话题时糊成毛球。
  // 真正被展开用过的节点（任一波的 seeds）。raw seeds 常是名字/未上画布的 key，
  // 必须解析成画布 key 再比——否则落选判断全错、锚点全灰
  const usedEver = useMemo(() => {
    const s = new Set<string>();
    waves.forEach((w) =>
      w.seeds.forEach((raw) => {
        if (nodes.has(raw)) s.add(raw);
        const k = nameToKeyRef.current.get(raw);
        if (k) s.add(k);
      }),
    );
    return s;
  }, [waves, nodes]);

  // 每步的种子集合（解析后）：展开边 = from 是该步种子的跨波边
  const seedsByWave = useMemo(() => {
    const m = new Map<number, Set<string>>();
    waves.forEach((w) => {
      const set = new Set<string>();
      w.seeds.forEach((raw) => {
        if (nodes.has(raw)) set.add(raw);
        const k = nameToKeyRef.current.get(raw);
        if (k) set.add(k);
      });
      m.set(w.wave, set);
    });
    return m;
  }, [waves, nodes]);

  // 参与跨波展开边的端点：折叠预览时优先保留，保证展开主链以真实节点连续可见
  const expandTouch = useMemo(() => {
    const s = new Set<string>();
    [...edges.values()].forEach((e) => {
      if (seedsByWave.get(e.wave)?.has(e.from)) {
        s.add(e.from);
        s.add(e.to);
      }
    });
    return s;
  }, [edges, seedsByWave]);

  const { layout, degree, hubKey, inner, clusters, waveSize, previewKeys } = useMemo(() => {
    const inW = Math.max(280, canvasSize.w - 24);
    const ids = [...nodes.keys()];
    const deg: Record<string, number> = {};
    const allEdges = [...edges.values()].filter((e) => nodes.has(e.from) && nodes.has(e.to));
    allEdges.forEach((e) => {
      deg[e.from] = (deg[e.from] || 0) + 1;
      deg[e.to] = (deg[e.to] || 0) + 1;
    });
    const emptyClusters: ClusterBox[] = [];
    if (!ids.length) {
      return {
        layout: {} as Record<string, Point>,
        degree: deg,
        hubKey: '',
        inner: { w: inW, h: Math.max(260, canvasSize.h - 24) },
        clusters: emptyClusters,
        waveSize: {} as Record<number, number>,
        previewKeys: {} as Record<number, Set<string>>,
      };
    }
    const waveOf = (k: string) => nodeWaveRef.current.get(k) ?? 0;
    const byWave = new Map<number, string[]>();
    ids.forEach((id) => {
      const w = waveOf(id);
      const arr = byWave.get(w);
      if (arr) arr.push(id);
      else byWave.set(w, [id]);
    });
    const waveList = [...byWave.keys()].sort((a, b) => a - b);
    // 纵向时间轴：单列、从上到下 = 推理顺序。行高全部由内容决定——
    // 框、骑缝步骤牌、节点标签都预留了配额，行与行之间不会相互压叠；
    // 跨波关系走贴框脊柱聚合线，不在主区穿线。
    const TOP = 96;
    const cxAll = inW / 2;
    let cum = TOP;
    const pos: Record<string, Point> = {};
    const clustersOut: ClusterBox[] = [];
    const waveSizeOut: Record<number, number> = {};
    const previewKeysOut: Record<number, Set<string>> = {};
    waveList.forEach((w) => {
      const cids = byWave.get(w) || [];
      waveSizeOut[w] = cids.length;
      const meta = waves.find((m) => m.wave === w);
      const seedSet = seedsByWave.get(w) || new Set<string>();
      const seed = [...seedSet].find((s) => cids.includes(s)) || null;
      const hub = cids.reduce((a, b) => ((deg[a] || 0) >= (deg[b] || 0) ? a : b), cids[0]);
      const isAnchors = meta?.kind === 'anchors';

      if (collapsed.has(w)) {
        // 折叠行：预览 ≤8 个沿正弦弧等距排布（种子/末位在中线、弧顶下垂），
        // +N 超节点紧跟末位槽位（不再甩到画布右缘，代理边也因此变短、不
        // 再横穿全屏）。弧形让同源边出射角互不相同，避免单排等高时叠成一束。
        // 优先级：本波种子 > 展开链端点 > 被用过 > 高度数；落选锚点压出预览位
        const score = (id: string) =>
          (seedSet.has(id) ? 1000 : 0) +
          (expandTouch.has(id) ? 700 : 0) +
          (usedEver.has(id) ? 500 : 0) +
          (deg[id] || 0) -
          (isAnchors && !usedEver.has(id) ? 400 : 0);
        const sorted = cids.slice().sort((a, b) => score(b) - score(a));
        const preview = sorted.slice(0, COLLAPSE_PREVIEW);
        previewKeysOut[w] = new Set(preview);
        const rest = cids.length - preview.length;
        const slots = preview.length + (rest > 0 ? 1 : 0);
        const slotW = Math.max(96, Math.min(124, (inW - 200) / Math.max(1, slots)));
        const left = cxAll - (slots * slotW) / 2 + slotW / 2;
        const h = 176;
        const nodeY = cum + 88;
        const lastSlot = Math.max(1, slots - 1);
        const arcY = (slot: number) =>
          nodeY + ARC_AMP * Math.sin(Math.PI * (slot / lastSlot)) + (slot % 2 ? 8 : -8);
        preview.forEach((id, i) => {
          pos[id] = { x: left + i * slotW, y: arcY(i) };
        });
        const proxy =
          rest > 0 ? { x: left + preview.length * slotW, y: arcY(preview.length) } : undefined;
        cids.forEach((id) => {
          if (!pos[id]) pos[id] = proxy || { x: cxAll, y: nodeY };
        });
        clustersOut.push({ wave: w, cx: cxAll, cy: cum + h / 2, top: cum, h, proxy });
        cum += h + ROW_GAP;
        return;
      }
      if (isAnchors) {
        // 锚点波：query 正下方扇形弧排布（>10 双层弧），全员带名字上画布
        const twoRows = cids.length > 10;
        const h = twoRows ? 250 : 200;
        const perRow = twoRows ? Math.ceil(cids.length / 2) : cids.length;
        cids.forEach((id, i) => {
          const row = twoRows && i >= perRow ? 1 : 0;
          const col = row === 0 ? i : i - perRow;
          const cols = row === 0 ? perRow : cids.length - perRow;
          const u = cols <= 1 ? 0.5 : col / (cols - 1);
          pos[id] = {
            x: 96 + u * (inW - 192),
            y: cum + 60 + row * 58 + Math.sin(Math.PI * u) * 24,
          };
        });
        clustersOut.push({ wave: w, cx: cxAll, cy: cum + h / 2, top: cum, h });
        cum += h + ROW_GAP;
        return;
      }
      // 展开波：原点系局部力导向，碰撞半径把标签占位算进去（种子/被用过
      // 的节点带光环+标注，再加余量），行高随内容自适应。
      const intra = allEdges
        .filter((e) => waveOf(e.from) === w && waveOf(e.to) === w)
        .map((e) => ({ source: e.from, target: e.to }));
      const colR: Record<string, number> = {};
      cids.forEach((id) => {
        colR[id] = LABEL_R + (seedSet.has(id) || usedEver.has(id) ? SEED_R_BONUS : 0);
      });
      const local = forceCluster(
        cids,
        intra,
        0,
        0,
        seed || hub,
        {},
        colR,
        waveLocalRef.current.get(w),
      );
      waveLocalRef.current.set(w, local);
      // 量测内容盒；过宽时横向压缩 + 纵向让位，绝不把间距压回去
      const bandW = inW - 176;
      let x0 = Infinity;
      let x1 = -Infinity;
      let y0 = Infinity;
      let y1 = -Infinity;
      const measure = () => {
        x0 = Infinity;
        x1 = -Infinity;
        y0 = Infinity;
        y1 = -Infinity;
        cids.forEach((id) => {
          const p = local[id];
          if (p.x < x0) x0 = p.x;
          if (p.x > x1) x1 = p.x;
          if (p.y < y0) y0 = p.y;
          if (p.y > y1) y1 = p.y;
        });
      };
      measure();
      if (x1 - x0 > bandW) {
        const k = bandW / (x1 - x0);
        cids.forEach((id) => {
          local[id] = { x: local[id].x * k, y: local[id].y };
        });
        separateLabels(cids, local, LABEL_R);
        measure();
      }
      const h = Math.min(760, Math.max(216, y1 - y0 + ROW_PAD_TOP + ROW_PAD_BOTTOM));
      const midX = (x0 + x1) / 2;
      cids.forEach((id) => {
        pos[id] = {
          x: Math.max(84, Math.min(inW - 84, cxAll + (local[id].x - midX))),
          y: cum + ROW_PAD_TOP + (local[id].y - y0),
        };
      });
      clustersOut.push({ wave: w, cx: cxAll, cy: cum + h / 2, top: cum, h });
      cum += h + ROW_GAP;
    });
    const globalHub = ids.reduce((a, b) => ((deg[a] || 0) >= (deg[b] || 0) ? a : b), ids[0]);
    return {
      layout: pos,
      degree: deg,
      hubKey: globalHub,
      inner: { w: inW, h: Math.max(canvasSize.h - 24, cum + 30) },
      clusters: clustersOut,
      waveSize: waveSizeOut,
      previewKeys: previewKeysOut,
    };
  }, [nodes, edges, canvasSize, waves, collapsed, usedEver, seedsByWave, expandTouch]);

  // 跨波边聚合 → 左侧脊柱连接线（一对波只画一条，带数量+主关系标签）
  const spine = useMemo(() => {
    const map = new Map<
      string,
      { a: number; b: number; count: number; rels: Record<string, number> }
    >();
    [...edges.values()].forEach((e) => {
      const wf = nodeWaveRef.current.get(e.from) ?? 0;
      const wt = nodeWaveRef.current.get(e.to) ?? 0;
      if (wf === wt) return;
      const a = Math.min(wf, wt);
      const b = Math.max(wf, wt);
      const key = `${a}|${b}`;
      const rec = map.get(key) || { a, b, count: 0, rels: {} };
      rec.count += 1;
      rec.rels[e.rel] = (rec.rels[e.rel] || 0) + 1;
      map.set(key, rec);
    });
    return [...map.values()];
  }, [edges]);

  // 新波上画布时平滑滚到该簇（仅运行中且未被用户接管）
  useEffect(() => {
    if (phase !== 'running' || !followRef.current) return;
    const el = canvasRef.current;
    const c = clusters[clusters.length - 1];
    if (!el || !c) return;
    progScrollRef.current = true;
    el.scrollTo({
      top: Math.max(0, c.cy * view.s + view.y - el.clientHeight * 0.55),
      behavior: 'smooth',
    });
    const tm = window.setTimeout(() => {
      progScrollRef.current = false;
    }, 700);
    return () => window.clearTimeout(tm);
    // 只在新波（currentWave）或波行数变化时跟随，避免拖拽/缩放触发回跳
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWave, clusters.length, phase]);

  // 生效坐标：用户拖过的节点用拖拽位，其余用布局位。边/包围盒/扇线
  // 全部读它，拖动时整张图保持一致跟随。
  const posOf = useCallback(
    (k: string): Point | null => userPos[k] || layout[k] || null,
    [userPos, layout],
  );

  // 边路由：可见端点接真实位置，折叠隐藏端点接该波 +N 超节点（代理路由），
  // 展开主链在任何折叠状态下都保持连续
  const routedEdges = useMemo(() => {
    const visOf = (k: string): NodeVis => {
      const w = nodeWaveRef.current.get(k) ?? 0;
      if (collapsed.has(w) && !(previewKeys?.[w]?.has(k) ?? false)) return 'proxied';
      return revealed.has(k) ? 'shown' : 'pending';
    };
    return routeEdges([...edges.values()], {
      waveOf: (k) => nodeWaveRef.current.get(k) ?? 0,
      visOf,
      nodePos: posOf,
      proxyPos: (w) => {
        const c = clusters.find((cc) => cc.wave === w);
        if (!c) return null;
        return c.proxy || { x: c.cx, y: c.cy };
      },
      isSeedOf: (w, k) => seedsByWave.get(w)?.has(k) ?? false,
    });
  }, [edges, posOf, clusters, collapsed, revealed, seedsByWave, previewKeys]);

  // 每波已显现内容的包围盒：步骤框、骑缝标题牌、脊柱、+N 全部锚定它，
  // 附属元素永远贴着真实内容而不是画布边缘。
  const waveBoxes = useMemo(() => {
    const out = new Map<number, { x0: number; x1: number; y0: number; y1: number }>();
    for (const n of nodes.values()) {
      const w = nodeWaveRef.current.get(n.key) ?? 0;
      if (collapsed.has(w) && !previewKeys[w]?.has(n.key)) continue;
      if (!revealed.has(n.key)) continue;
      const p = posOf(n.key);
      if (!p) continue;
      const b = out.get(w);
      if (!b) out.set(w, { x0: p.x, x1: p.x, y0: p.y, y1: p.y });
      else {
        b.x0 = Math.min(b.x0, p.x);
        b.x1 = Math.max(b.x1, p.x);
        b.y0 = Math.min(b.y0, p.y);
        b.y1 = Math.max(b.y1, p.y);
      }
    }
    // 折叠行的 +N 超节点也是内容，框要把它围进来
    clusters.forEach((c) => {
      if (!c.proxy || !collapsed.has(c.wave)) return;
      const b = out.get(c.wave);
      if (!b) return;
      b.x1 = Math.max(b.x1, c.proxy.x + 18);
      b.y0 = Math.min(b.y0, c.proxy.y);
      b.y1 = Math.max(b.y1, c.proxy.y);
    });
    return out;
  }, [nodes, posOf, clusters, collapsed, previewKeys, revealed]);

  // 展开边成扇整理：同一起点指向同一波的边按目标 x 排序、错开曲率，
  // 近源端不再重合；步骤牌只挂在扇形中间那条上（同波数字相同，
  // 逐条标注是纯噪音）。
  const edgeFan = useMemo(() => {
    const groups = new Map<string, RoutedEdge[]>();
    routedEdges.forEach((re) => {
      if (re.kind !== 'expand') return;
      const gk = `${re.fromProxy ? `p${re.wf}` : re.from}|${re.wave}|${re.wt}`;
      const g = groups.get(gk);
      if (g) g.push(re);
      else groups.set(gk, [re]);
    });
    const out = new Map<string, { off: number; badge: boolean }>();
    groups.forEach((list) => {
      list.sort((a, b) => a.b.x - b.b.x);
      const mid = Math.floor((list.length - 1) / 2);
      list.forEach((re, i) => {
        out.set(re.key, {
          off: Math.max(-64, Math.min(64, (i - (list.length - 1) / 2) * 16)),
          badge: i === mid,
        });
      });
    });
    return out;
  }, [routedEdges]);

  if (useFallback && fallback) {
    return (
      <>
        {errorMsg ? (
          <div className={styles.fallbackBanner}>
            <WarningOutlined /> {t('graphCenter.exploreFallback')}（{errorMsg}）
          </div>
        ) : null}
        {fallback}
      </>
    );
  }

  const done = phase === 'final' || phase === 'error';
  return (
    <div className={styles.stage}>
      <div className={styles.head}>
        <button type="button" className={styles.backBtn} onClick={onBack}>
          <ArrowLeftOutlined /> {t('graphCenter.journeyBack')}
        </button>
        <div className={styles.headTitle}>
          <span className={styles.headBadge}>
            <RobotOutlined /> {t('graphCenter.exploreTitle')}
          </span>
          <b>{query}</b>
          {model ? <span className={styles.headModel}>{model}</span> : null}
        </div>
        <div className={styles.headRight}>
          {!done ? (
            <span className={styles.pulse}>{t('graphCenter.exploreRunning')}</span>
          ) : (
            <button
              type="button"
              className={styles.replayBtn}
              onClick={() => setRunId((r) => r + 1)}
            >
              {t('graphCenter.journeyReplay')}
            </button>
          )}
        </div>
      </div>

      <div className={styles.body}>
        {/* 左：增量画布（沿用 journey 场景美术风格） */}
        <div
          className={styles.canvas}
          ref={canvasRef}
          onScroll={() => {
            if (!progScrollRef.current) followRef.current = false;
          }}
        >
          {/* 节点详情：sticky 悬浮，滚动长画布时仍可见 */}
          {selected ? (
            <div className={styles.detail}>
              <div className={styles.detailHead}>
                <b>{selected.name || selected.key}</b>
                <span
                  className={styles.detailLabel}
                  style={{ background: LABEL_COLOR[selected.label] || DEFAULT_COLOR }}
                >
                  {selected.label}
                </span>
                <button type="button" onClick={() => setSelected(null)}>
                  ✕
                </button>
              </div>
              <code className={styles.detailKey}>{selected.key}</code>
              <div className={styles.detailProps}>
                {Object.entries(selected.props || {})
                  .slice(0, 12)
                  .map(([k, v]) => (
                    <div key={k}>
                      <span>{k}: </span>
                      {String(v).slice(0, 120)}
                    </div>
                  ))}
              </div>
            </div>
          ) : null}
          {/* 推理波轨道：点某波 → 只看这一步加了什么（其余调光） */}
          {waves.length ? (
            <div className={styles.waveRail}>
              <button
                type="button"
                className={`${styles.waveChip} ${focusWave == null ? styles.waveChipActive : ''}`}
                onClick={() => setFocusWave(null)}
              >
                全部
              </button>
              {waves.map((w) => (
                <button
                  key={w.wave}
                  type="button"
                  className={`${styles.waveChip} ${focusWave === w.wave ? styles.waveChipActive : ''}`}
                  onClick={() => setFocusWave((f) => (f === w.wave ? null : w.wave))}
                >
                  W{w.wave}{' '}
                  {w.kind === 'anchors'
                    ? `锚点×${w.nodeCount}`
                    : `+${w.nodeCount}点/${w.edgeCount}边`}
                </button>
              ))}
            </div>
          ) : null}
          <div className={styles.sceneBox}>
            {nodes.size === 0 ? (
              <div className={styles.canvasEmpty}>
                {done ? t('graphCenter.exploreNoGraph') : t('graphCenter.exploreCanvasWaiting')}
              </div>
            ) : (
              <svg
                width={inner.w}
                height={inner.h}
                ref={setSvgEl}
                style={{ cursor: dragRef.current?.moved ? 'grabbing' : 'grab' }}
                onPointerDown={(ev) => {
                  if (ev.button !== 0) return;
                  // 注意：不能 setPointerCapture——捕获后 click 会全派发到 svg，
                  // 超节点/标题/节点的 onClick 就全废了
                  dragRef.current = {
                    px: ev.clientX,
                    py: ev.clientY,
                    x: view.x,
                    y: view.y,
                    moved: false,
                  };
                }}
                onPointerMove={(ev) => {
                  const nd = nodeDragRef.current;
                  if (nd) {
                    // 节点拖拽：client 位移除以缩放换算回内容坐标
                    const ndx = ev.clientX - nd.px;
                    const ndy = ev.clientY - nd.py;
                    if (Math.abs(ndx) + Math.abs(ndy) > 3) nd.moved = true;
                    if (nd.moved) {
                      setUserPos((prev) => ({
                        ...prev,
                        [nd.key]: { x: nd.x + ndx / view.s, y: nd.y + ndy / view.s },
                      }));
                    }
                    return;
                  }
                  const d = dragRef.current;
                  if (!d) return;
                  const dx = ev.clientX - d.px;
                  const dy = ev.clientY - d.py;
                  if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
                  if (d.moved) {
                    followRef.current = false;
                    setView((v) => ({ ...v, x: d.x + dx, y: d.y + dy }));
                  }
                }}
                onPointerUp={() => {
                  window.setTimeout(() => {
                    dragRef.current = null;
                    nodeDragRef.current = null;
                  }, 0);
                }}
              >
                <g transform={`translate(${view.x} ${view.y}) scale(${view.s})`}>
                  {/* query 节点：纵向时间轴顶部居中 */}
                  <g>
                    <circle cx={inner.w / 2} cy={44} r={17} fill="#8b5cf6" opacity=".15" />
                    <circle
                      cx={inner.w / 2}
                      cy={44}
                      r={24}
                      fill="none"
                      stroke="#0D76FD"
                      strokeWidth={2}
                      className={styles.queryPulse}
                    />
                    <circle cx={inner.w / 2} cy={44} r={5.5} fill="#8b5cf6" />
                    <text x={inner.w / 2} y={78} textAnchor="middle" className={styles.nodeLabel}>
                      {t('graphCenter.journeyQueryNode')}
                    </text>
                  </g>
                  {/* query → 每个已显现锚点一条短虚线（锚点行就在正下方，线短不交叉） */}
                  {anchorKeysRef.current.map((k) => {
                    const p = posOf(k);
                    if (!p || !revealed.has(k)) return null;
                    // 落选锚点不连扇线——只保留模型真正走过的路，画面减噪
                    if (!usedEver.has(k)) return null;
                    return (
                      <path
                        key={`ql-${k}`}
                        d={`M ${inner.w / 2} 60 Q ${(inner.w / 2 + p.x) / 2} ${(60 + p.y) / 2 - 12} ${p.x} ${p.y}`}
                        fill="none"
                        stroke="#8b5cf6"
                        strokeWidth={1.1}
                        strokeDasharray="4 5"
                        opacity={
                          focusWave != null && focusWave !== (nodeWaveRef.current.get(k) ?? 0)
                            ? 0.08
                            : 0.3
                        }
                        className={styles.edgeFade}
                      />
                    );
                  })}
                  {/* 贴框脊柱：跨波边聚合成一对波一条线（数量+主关系），
                    两端锚在步骤框左缘中点，不再悬空在画布左侧 */}
                  {spine.map((s) => {
                    const ra = clusters.find((c) => c.wave === s.a);
                    const rb = clusters.find((c) => c.wave === s.b);
                    if (!ra || !rb) return null;
                    const ba = waveBoxes.get(s.a);
                    const bb = waveBoxes.get(s.b);
                    const ax = (ba ? ba.x0 : ra.cx) - 30;
                    const bx = (bb ? bb.x0 : rb.cx) - 30;
                    const railX = Math.max(14, Math.min(ax, bx) - 34);
                    const color = WAVE_COLORS[s.b % WAVE_COLORS.length];
                    const dim = focusWave != null && focusWave !== s.a && focusWave !== s.b;
                    const topRel = Object.entries(s.rels).sort((x, y) => y[1] - x[1])[0];
                    const midY = (ra.cy + rb.cy) / 2;
                    return (
                      <g key={`spine-${s.a}-${s.b}`} opacity={dim ? 0.12 : 0.85}>
                        <path
                          d={`M ${ax} ${ra.cy} C ${railX} ${ra.cy + 30} ${railX} ${rb.cy - 30} ${bx} ${rb.cy}`}
                          fill="none"
                          stroke={color}
                          strokeWidth={1.6}
                          strokeDasharray="6 4"
                          className={styles.edgeFade}
                        />
                        <polygon
                          points={`${bx + 2},${rb.cy} ${bx - 8},${rb.cy - 5} ${bx - 8},${rb.cy + 5}`}
                          fill={color}
                        />
                        <text x={railX + 8} y={midY + 3} className={styles.spineLabel}>
                          ×{s.count} {topRel ? topRel[0] : ''}
                        </text>
                      </g>
                    );
                  })}
                  {/* 相邻步骤间的时间轴连接线：即使某步 0 条边也不会悬空成孤岛 */}
                  {clusters.map((c, ci) => {
                    const prev = clusters[ci - 1];
                    if (!prev) return null;
                    const pb = waveBoxes.get(prev.wave);
                    const cb = waveBoxes.get(c.wave);
                    if (!pb || !cb) return null;
                    const x = inner.w / 2;
                    const yTop = pb.y1 + 40;
                    const yBot = cb.y0 - 36;
                    if (yBot - yTop < 6) return null;
                    const dimmed =
                      focusWave != null && focusWave !== c.wave && focusWave !== prev.wave;
                    return (
                      <g
                        key={`tl-${prev.wave}-${c.wave}`}
                        opacity={dimmed ? 0.1 : 0.55}
                        pointerEvents="none"
                      >
                        <line
                          x1={x}
                          y1={yTop}
                          x2={x}
                          y2={yBot}
                          stroke="#b9c3d6"
                          strokeWidth={1.4}
                          strokeDasharray="2 5"
                        />
                        <polygon
                          points={`${x},${yBot + 3} ${x - 4},${yBot - 5} ${x + 4},${yBot - 5}`}
                          fill="#b9c3d6"
                        />
                      </g>
                    );
                  })}
                  {/* 步骤包围盒：贴内容的虚线框 + 左上角骑缝步骤牌（牌骑在框缘上，
                    行高已为它留配额，不会压到上一行） */}
                  {clusters.map((c) => {
                    const m = waves.find((w) => w.wave === c.wave);
                    const b = waveBoxes.get(c.wave);
                    if (!m || !b) return null;
                    const color = WAVE_COLORS[c.wave % WAVE_COLORS.length];
                    const dimmed = focusWave != null && focusWave !== c.wave;
                    const seedName = m.seeds.length
                      ? String(nodes.get(m.seeds[0])?.name || m.seeds[0]).slice(0, 8)
                      : '';
                    const stepNo = waves.findIndex((w) => w.wave === c.wave) + 1;
                    const label = `第${stepNo}步 · ${TOOL_LABEL[m.tool] || m.tool}${m.kind === 'anchors' ? ` ×${m.nodeCount}` : seedName ? ` · 自「${seedName}…」` : ''} ${collapsed.has(c.wave) ? '· 展开' : '· 收起'}`;
                    const chipW = Math.min(inner.w - 16, label.length * 10.5 + 26);
                    // 框宽不得小于骑缝牌宽（牌两侧各留 10px）；窄内容行把补足量
                    // 对称分摊到两侧，节点保持居中而不是缩在框的最左端。
                    const contentW = Math.max(120, b.x1 - b.x0 + 56);
                    const extra = Math.max(0, chipW + 20 - contentW);
                    const bx = Math.max(8, b.x0 - 28 - extra / 2);
                    const by = b.y0 - 34;
                    const bw = contentW + extra;
                    const chipX = Math.max(8, Math.min(bx + 10, inner.w - chipW - 8));
                    return (
                      <g key={`hull-${c.wave}`} opacity={dimmed ? 0.15 : 1}>
                        <rect
                          x={bx}
                          y={by}
                          width={bw}
                          height={b.y1 - b.y0 + 72}
                          rx={16}
                          fill={color}
                          fillOpacity={0.05}
                          stroke={color}
                          strokeOpacity={0.55}
                          strokeWidth={1.3}
                          strokeDasharray="7 5"
                          pointerEvents="none"
                        />
                        <g
                          style={{ cursor: 'pointer' }}
                          onClick={() => {
                            if (dragRef.current?.moved) return;
                            toggleWave(c.wave);
                          }}
                        >
                          <rect
                            x={chipX}
                            y={by - 10}
                            width={chipW}
                            height={20}
                            rx={10}
                            fill={color}
                          />
                          <text x={chipX + 10} y={by + 4} className={styles.stepChip}>
                            {label}
                          </text>
                        </g>
                      </g>
                    );
                  })}
                  {routedEdges.map((re) => {
                    // ref（非展开跨波）边只在 focus 到相关波时作参考线，平时由脊柱聚合表达
                    if (
                      re.kind === 'ref' &&
                      !(focusWave != null && (re.wf === focusWave || re.wt === focusWave))
                    )
                      return null;
                    const { a, b } = re;
                    const fan = edgeFan.get(re.key);
                    const mx = (a.x + b.x) / 2;
                    const my = (a.y + b.y) / 2;
                    const dx = b.x - a.x;
                    const dy = b.y - a.y;
                    const len = Math.hypot(dx, dy) || 1;
                    // 同源扇形逐条错开曲率；非扇形边保留原来的微弧
                    const off = fan ? fan.off : Math.min(26, len * 0.16);
                    const cx = mx - (dy / len) * off;
                    const cy = my + (dx / len) * off;
                    const d = `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`;
                    const color = WAVE_COLORS[re.wave % WAVE_COLORS.length];
                    // 只有最新一波（或被 focus 的波）流动；旧展开边退为实色静态
                    const flowing =
                      re.kind !== 'ref' && (re.wave === currentWave || focusWave === re.wave);
                    const emphasized = re.kind === 'expand' && (pulseKeys.has(re.from) || flowing);
                    let opacity: number;
                    let stroke = color;
                    let dash: string | undefined;
                    if (re.kind === 'ref') {
                      stroke = '#94a3b8';
                      dash = '3 5';
                      opacity = 0.35;
                    } else if (focusWave != null) {
                      opacity =
                        re.wave === focusWave
                          ? flowing
                            ? 0.95
                            : 0.75
                          : re.wf === focusWave || re.wt === focusWave
                            ? 0.15
                            : 0.05;
                    } else {
                      opacity = flowing ? 0.95 : re.kind === 'expand' ? 0.6 : 0.38;
                    }
                    const stepNo = waves.findIndex((w) => w.wave === re.wave) + 1;
                    const midX = 0.25 * a.x + 0.5 * cx + 0.25 * b.x;
                    const midY = 0.25 * a.y + 0.5 * cy + 0.25 * b.y;
                    return (
                      <g key={re.key}>
                        <path
                          d={d}
                          pathLength={200}
                          fill="none"
                          stroke={stroke}
                          strokeWidth={
                            flowing ? (emphasized ? 2.6 : 1.8) : re.kind === 'expand' ? 1.6 : 1.1
                          }
                          strokeDasharray={dash}
                          opacity={opacity}
                          className={styles.edgeDraw}
                        />
                        {flowing ? (
                          <path
                            d={d}
                            fill="none"
                            stroke={color}
                            strokeWidth={emphasized ? 3 : 2.4}
                            className={styles.edgeFlow}
                          />
                        ) : null}
                        {re.kind === 'expand'
                          ? (() => {
                              // 方向箭头：沿控制点→终点切线，落在目标边缘（代理端留更大退距）
                              const el = Math.hypot(b.x - cx, b.y - cy) || 1;
                              const ux = (b.x - cx) / el;
                              const uy = (b.y - cy) / el;
                              const back = re.toProxy ? 20 : 13;
                              const tx = b.x - ux * back;
                              const ty = b.y - uy * back;
                              return (
                                <polygon
                                  points={`${tx},${ty} ${tx - ux * 7 - uy * 4.5},${ty - uy * 7 + ux * 4.5} ${tx - ux * 7 + uy * 4.5},${ty - uy * 7 - ux * 4.5}`}
                                  fill={color}
                                  opacity={Math.min(0.9, opacity + 0.15)}
                                />
                              );
                            })()
                          : null}
                        {re.kind === 'expand' && (flowing || re.count > 1) ? (
                          <text
                            x={midX}
                            y={midY - 12}
                            textAnchor="middle"
                            className={styles.edgeLabelIn}
                          >
                            {re.count > 1 ? `×${re.count} ` : ''}
                            {re.rel}
                          </text>
                        ) : null}
                        {re.kind === 'expand' &&
                        stepNo > 0 &&
                        opacity >= 0.5 &&
                        (edgeFan.get(re.key)?.badge ?? true) ? (
                          <g pointerEvents="none">
                            {/* 边上步骤牌：每个展开扇只标中间一条，避免同号牌堆叠 */}
                            <circle
                              cx={midX}
                              cy={midY}
                              r={7}
                              fill={color}
                              stroke="#fff"
                              strokeWidth={1.3}
                            />
                            <text
                              x={midX}
                              y={midY + 3}
                              textAnchor="middle"
                              className={styles.stepBadge}
                            >
                              {stepNo}
                            </text>
                          </g>
                        ) : null}
                      </g>
                    );
                  })}
                  {[...nodes.values()].map((n) => {
                    const nw = nodeWaveRef.current.get(n.key) ?? 0;
                    if (collapsed.has(nw) && !previewKeys[nw]?.has(n.key)) return null;
                    const p = posOf(n.key);
                    if (!p || !revealed.has(n.key)) return null;
                    const color = LABEL_COLOR[n.label] || DEFAULT_COLOR;
                    const r = 7 + Math.min(6, degree[n.key] || 0);
                    const hub = n.key === hubKey;
                    const dimmed = focusWave != null && nw !== focusWave;
                    // 落选锚点（从未被任何一步选作种子）→ 标灰 + 删除线，类似隐藏
                    const wMeta = waves.find((w) => w.wave === nw);
                    const unused = wMeta?.kind === 'anchors' && !usedEver.has(n.key);
                    const used = usedEver.has(n.key);
                    const stepNo = waves.findIndex((w) => w.wave === nw) + 1;
                    // 标签默认全量展示（碰撞半径已把标签占位算进去），超大波才降级
                    const showLabel =
                      (waveSize[nw] || 0) <= 40 ||
                      wMeta?.kind === 'anchors' ||
                      hub ||
                      pulseKeys.has(n.key) ||
                      selected?.key === n.key;
                    return (
                      <g
                        key={n.key}
                        className={styles.pop}
                        opacity={dimmed ? 0.13 : unused ? 0.45 : 1}
                        style={{ cursor: done ? 'grab' : 'pointer' }}
                        onPointerDown={(ev) => {
                          // 图定格（探索结束）后才允许自由拖动；运行中布局还在
                          // 增量重算，拖了也会被下一轮覆盖。
                          if (!done || ev.button !== 0) return;
                          ev.stopPropagation();
                          nodeDragRef.current = {
                            key: n.key,
                            px: ev.clientX,
                            py: ev.clientY,
                            x: p.x,
                            y: p.y,
                            moved: false,
                          };
                        }}
                        onClick={() => {
                          if (dragRef.current?.moved || nodeDragRef.current?.moved) return;
                          setSelected(selected?.key === n.key ? null : n);
                        }}
                      >
                        {!unused ? (
                          <circle cx={p.x} cy={p.y} r={r + 4} fill={color} opacity=".14" />
                        ) : null}
                        <circle
                          cx={p.x}
                          cy={p.y}
                          r={r}
                          fill={unused ? '#eef1f6' : '#fff'}
                          stroke={unused ? '#9aa4b8' : color}
                          strokeWidth={hub || used ? 2.6 : 1.8}
                          strokeDasharray={unused ? '4 3' : undefined}
                          className={`${hub && !unused ? styles.anchorGlow : ''} ${pulseKeys.has(n.key) ? styles.srcPulse : ''}`}
                        />
                        {used && !unused ? (
                          // 已被模型选作展开种子：常驻高亮环（原型 chosen selring 轻量版）
                          <circle
                            cx={p.x}
                            cy={p.y}
                            r={r + 4.5}
                            fill="none"
                            stroke={color}
                            strokeWidth={1.5}
                            strokeDasharray="3 3"
                            className={styles.usedRing}
                          />
                        ) : null}
                        {pulseKeys.has(n.key) ? (
                          <>
                            {/* 展开起点动效标注：两次扩散回波 + 常驻旋转虚线光环 + 小标签 */}
                            <circle cx={p.x} cy={p.y} r={r + 5} className={styles.seedRing} />
                            <circle cx={p.x} cy={p.y} r={r + 5} className={styles.seedRingLate} />
                            <circle cx={p.x} cy={p.y} r={r + 5} className={styles.seedHalo} />
                            <text
                              x={p.x}
                              y={p.y - r - 9}
                              textAnchor="middle"
                              className={styles.seedTag}
                            >
                              展开起点
                            </text>
                          </>
                        ) : null}
                        {!unused && (hub || pulseKeys.has(n.key)) ? (
                          <g pointerEvents="none">
                            {/* 步骤徽章：只标 hub/种子——同波角标数字相同，全量展示是纯噪音；
                              步骤归属由包围盒标题牌 + 边上步骤牌承载 */}
                            <circle
                              cx={p.x - r * 0.72 - 4}
                              cy={p.y - r * 0.72 - 4}
                              r={7}
                              fill={WAVE_COLORS[nw % WAVE_COLORS.length]}
                              stroke="#fff"
                              strokeWidth={1.4}
                            />
                            <text
                              x={p.x - r * 0.72 - 4}
                              y={p.y - r * 0.72 - 0.5}
                              textAnchor="middle"
                              className={styles.stepBadge}
                            >
                              {stepNo}
                            </text>
                          </g>
                        ) : null}
                        {showLabel ? (
                          <text
                            x={p.x}
                            y={p.y + r + 12}
                            textAnchor="middle"
                            className={styles.nodeLabel}
                            style={
                              unused
                                ? { fill: '#8b95ad', textDecoration: 'line-through' }
                                : undefined
                            }
                          >
                            {(n.name || n.key).length > 9
                              ? `${(n.name || n.key).slice(0, 9)}…`
                              : n.name || n.key}
                          </text>
                        ) : null}
                      </g>
                    );
                  })}
                  {/* 折叠行末位 +N 气泡：紧跟预览节点，点击展开整行 */}
                  {clusters.map((c) => {
                    if (!collapsed.has(c.wave) || !c.proxy) return null;
                    const total = waveSize[c.wave] || 0;
                    const shown = previewKeys[c.wave]?.size || 0;
                    const rest = total - shown;
                    if (rest <= 0) return null;
                    const color = WAVE_COLORS[c.wave % WAVE_COLORS.length];
                    const dim = focusWave != null && focusWave !== c.wave;
                    return (
                      <g
                        key={`more-${c.wave}`}
                        opacity={dim ? 0.25 : 1}
                        style={{ cursor: 'pointer' }}
                        onClick={() => {
                          if (dragRef.current?.moved) return;
                          toggleWave(c.wave);
                        }}
                      >
                        <circle
                          cx={c.proxy.x}
                          cy={c.proxy.y}
                          r={16}
                          fill="#fff"
                          stroke={color}
                          strokeWidth={1.8}
                          strokeDasharray="5 4"
                          className={styles.superSpin}
                        />
                        <text
                          x={c.proxy.x}
                          y={c.proxy.y + 4}
                          textAnchor="middle"
                          className={styles.superCount}
                        >
                          +{rest}
                        </text>
                        <text
                          x={c.proxy.x}
                          y={c.proxy.y + 30}
                          textAnchor="middle"
                          className={styles.superHint}
                        >
                          展开
                        </text>
                      </g>
                    );
                  })}
                </g>
              </svg>
            )}
            {narration ? (
              <div className={styles.narration} key={narration}>
                {narration}
              </div>
            ) : null}
          </div>
          {/* 缩放控制：sticky 吸底，滚轮缩放 + 拖拽平移的兜底按钮 */}
          <div className={styles.zoomCtl}>
            <button
              type="button"
              title="放大"
              onClick={() => setView((v) => ({ ...v, s: Math.min(3, v.s * 1.2) }))}
            >
              <ZoomInOutlined />
            </button>
            <button
              type="button"
              title="缩小"
              onClick={() => setView((v) => ({ ...v, s: Math.max(0.35, v.s / 1.2) }))}
            >
              <ZoomOutOutlined />
            </button>
            <button type="button" title="复位" onClick={() => setView({ x: 0, y: 0, s: 1 })}>
              <ReloadOutlined />
            </button>
          </div>
        </div>

        {/* 右：模型推理过程卡片 */}
        <div className={styles.traceCard}>
          <div className={styles.traceHead}>
            <span>
              <BulbOutlined /> {t('graphCenter.exploreTraceTitle')}
            </span>
            <span className={styles.traceCount}>
              {trace.filter((e) => e.kind === 'call').length} {t('graphCenter.exploreTraceCalls')}
            </span>
          </div>
          <div className={styles.traceBody}>
            {phase === 'connecting' ? (
              <div className={styles.dim}>{t('graphCenter.exploreConnecting')}</div>
            ) : null}
            {trace.map((entry) =>
              entry.kind === 'thinking' ? (
                <div key={entry.key} className={styles.think}>
                  <span className={styles.thinkTag}>
                    <BulbOutlined /> {t('graphCenter.exploreTraceRound', { num: entry.round })}
                  </span>
                  <div className={styles.thinkText}>
                    <MiniMarkdown text={entry.text || ''} />
                  </div>
                </div>
              ) : (
                <div
                  key={entry.key}
                  className={`${styles.call} ${entry.status === 'error' ? styles.callErr : ''}`}
                >
                  <div className={styles.callHead}>
                    <span className={styles.callStatus}>
                      {entry.status === 'running' ? (
                        <LoadingOutlined spin />
                      ) : entry.status === 'ok' ? (
                        <CheckCircleFilled />
                      ) : (
                        <ExclamationCircleFilled />
                      )}
                    </span>
                    <code>{entry.name}</code>
                    <span className={styles.callHint}>{TOOL_LABEL[entry.name || ''] || ''}</span>
                  </div>
                  {entry.args && Object.keys(entry.args).length ? (
                    <JsonBlock value={entry.args} compact />
                  ) : null}
                  {entry.status && entry.status !== 'running' ? (
                    <div className={styles.callResult}>
                      {entry.resultSummary}
                      {entry.graphCounts && entry.graphCounts.nodes > 0 ? (
                        <span className={styles.canvasTag}>
                          画布 +{entry.graphCounts.nodes}节点
                          {entry.graphCounts.edges ? ` +${entry.graphCounts.edges}边` : ''}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  {entry.raw ? (
                    <details className={styles.callRaw}>
                      <summary>查看完整返回（模型看到的）</summary>
                      <JsonBlock value={entry.raw} />
                    </details>
                  ) : null}
                </div>
              ),
            )}
            {budgetReason ? <div className={styles.budget}>⛔ {budgetReason}</div> : null}
            {phase === 'error' ? <div className={styles.error}>✗ {errorMsg}</div> : null}
            <div ref={traceEndRef} />
          </div>
        </div>
      </div>

      {/* 底：最终回答 —— 默认收成一条细栏，展开为浮动抽屉，不挤压画布 */}
      {finalText ? (
        <>
          <button type="button" className={styles.finalBar} onClick={() => setFinalOpen((o) => !o)}>
            <span className={styles.finalBarTitle}>✓ {t('graphCenter.exploreFinal')}</span>
            {finalMeta ? (
              <span className={styles.finalMeta}>
                {finalMeta.rounds} 轮 · {finalMeta.tool_calls} 次工具调用 ·{' '}
                {(finalMeta.elapsed_ms / 1000).toFixed(1)}s
              </span>
            ) : null}
            <span className={styles.finalChevron}>{finalOpen ? '▾ 收起' : '▴ 展开'}</span>
          </button>
          {finalOpen ? (
            <div className={styles.finalSheet}>
              <div className={styles.finalSheetHead}>
                <b>✓ {t('graphCenter.exploreFinal')}</b>
                <button type="button" onClick={() => setFinalOpen(false)}>
                  ✕
                </button>
              </div>
              <MiniMarkdown text={finalText} />
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
