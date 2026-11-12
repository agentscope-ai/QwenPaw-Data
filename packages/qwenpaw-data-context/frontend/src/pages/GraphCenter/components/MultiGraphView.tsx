/** MultiGraphView — React port of Graph.renderMulti() from cm-graph-demo/js/graph.js.
 *  Renders the four layer clusters (cell membranes) with ports, cross-layer
 *  ribbons (aggregated gradient bands with flow animation), hull outlines and
 *  DOM chips floating above each cluster membrane. */
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ExtensionCategory, Graph, GraphEvent, Quadratic, register } from '@antv/g6';
import type { CustomPluginOption, EdgeData, IElementEvent, NodeData, Point } from '@antv/g6';
import type { Group } from '@antv/g';
import type { BaseEdgeStyleProps } from '@antv/g6';
import type { LayerId } from '../types';
import { LAYER_COLOR, LAYER_SHAPE, relLabel } from '../dataset/constants';
import {
  isPrivateNode,
  nodeOwnerId,
  typeLabel,
  layerLabel,
  type GraphDb,
} from '../dataset/graphDb';
import styles from './multigraph.module.less';

export interface MultiNodeDatum {
  id: string;
  label: string;
  type: string;
  layer: LayerId;
  x: number;
  y: number;
  focus?: boolean;
  hot?: boolean;
  dim?: boolean;
  /** Member mirror-add (owner != __global__) — dashed outline. */
  priv?: boolean;
}

export interface PortDatum {
  id: string;
  layer: LayerId;
  x: number;
  y: number;
  dim?: boolean;
}

export interface IntraEdgeDatum {
  source: string;
  target: string;
  relation: string;
  dim?: boolean;
}

export interface RibbonDatum {
  pair: string;
  source: string;
  target: string;
  count: number;
  expanded?: boolean;
  dim?: boolean;
  colorA: string;
  colorB: string;
  curveOffset?: number;
  sx: number;
  sy: number;
  tx: number;
  ty: number;
}

export interface CrossEdgeDatum {
  source: string;
  target: string;
  relation: string;
  colorA: string;
  colorB: string;
  curveOffset?: number;
  sx: number;
  sy: number;
  tx: number;
  ty: number;
}

export interface ChipDatum {
  layer: LayerId;
  label: string;
  shown: number;
  total: number;
  done: boolean;
  shrinkDone: boolean;
  /** Business mode: hide +/- on TG (Task roots only). */
  hideHop?: boolean;
}

export interface MultiGraphData {
  nodes: MultiNodeDatum[];
  ports: PortDatum[];
  intra: IntraEdgeDatum[];
  ribbons: RibbonDatum[];
  cross: CrossEdgeDatum[];
}

export interface MultiGraphHandle {
  fit: () => void;
  /** Export the canvas as a PNG download. */
  exportPNG: (filename?: string) => void;
}

interface MultiGraphViewProps {
  /** Query API over the currently loaded dataset (real scope or demo). */
  db: GraphDb;
  data: MultiGraphData;
  members: Partial<Record<LayerId, string[]>>;
  chips: ChipDatum[];
  /** Center/focus this node once ready (Locate flow). */
  locateId?: string | null;
  /** Increment to re-fit the viewport (reset / scope change). Hop updates keep the camera. */
  cameraNonce?: number;
  onNodeClick?: (nodeId: string) => void;
  onNodeDblClick?: (nodeId: string) => void;
  onRibbonClick?: (pair: string) => void;
  onGrow?: (layer: LayerId) => void;
  onShrink?: (layer: LayerId) => void;
  onReady?: (handle: MultiGraphHandle) => void;
}

const LAYERS: LayerId[] = ['MG', 'KG', 'UG'];
const MIN_LABEL_CHARS = 12;
const MAX_LABEL_CHARS = 24;

function sizeFor(layer: LayerId, focus: boolean, deg: number): number {
  let base = Math.max(32, Math.min(56, 32 + (deg || 0) * 3));
  if (layer === 'KG') base += 16;
  if (focus) base += 8;
  return base;
}

function truncateMiddle(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  const headLen = Math.max(4, Math.floor((maxChars - 1) * 0.45));
  const tailLen = Math.max(4, maxChars - headLen - 1);
  return text.slice(0, headLen) + '…' + text.slice(-tailLen);
}

function clipLabel(s: unknown, nodeSize: number): string {
  const v = String(s == null ? '' : s);
  const sz = nodeSize || 42;
  const maxChars = Math.max(MIN_LABEL_CHARS, Math.min(MAX_LABEL_CHARS, Math.floor(sz / 3)));
  if (v.length <= maxChars) return v;
  const segments = v.split(':').filter(Boolean);
  if (segments.length >= 3) {
    const tail = segments.slice(-2).join(':');
    if (tail.length <= maxChars) return tail;
    return truncateMiddle(tail, maxChars);
  }
  return truncateMiddle(v, maxChars);
}

function esc(s: unknown): string {
  return String(s == null ? '' : s).replace(
    /[&<>"]/g,
    (c) =>
      (({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }) as Record<string, string>)[c],
  );
}

/** Source-cluster color -> target-cluster color linear gradient (angle follows
 *  the line direction). Replaces the old 6-pair color lookup table. */
function gradient(a: string, b: string, sx: number, sy: number, tx: number, ty: number): string {
  const deg = Math.round(((Math.atan2(ty - sy, tx - sx) * 180) / Math.PI + 360) % 360);
  return `l(${deg}) 0:${a} 1:${b}`;
}

// ── Custom flow edge (cross-graph ribbon particle animation) ──
// Official ant-line pattern: Quadratic + infinite lineDashOffset animation;
// registered once at module level. The npm build exposes the `render` hook
// (the vendored demo used `onCreate`).
let flowEdgeReady: boolean | null = null;
function ensureFlowEdge(): boolean {
  if (flowEdgeReady != null) return flowEdgeReady;
  flowEdgeReady = false;
  try {
    class FlowQuadratic extends Quadratic {
      render(attributes?: Required<BaseEdgeStyleProps>, container?: Group) {
        super.render(attributes, container);
        const key = this.shapeMap?.key as unknown as
          | {
              animate?: (
                frames: unknown[],
                options: { duration: number; iterations: number },
              ) => unknown;
            }
          | undefined;
        key?.animate?.([{ lineDashOffset: 0 }, { lineDashOffset: -52 }], {
          duration: 2800,
          iterations: Infinity,
        });
      }
    }
    register(ExtensionCategory.EDGE, 'flow-quadratic', FlowQuadratic);
    flowEdgeReady = true;
  } catch {
    // Already registered (HMR) or unsupported — ribbons render without flow.
    flowEdgeReady = true;
  }
  return flowEdgeReady;
}

function nodeTooltipPlugin(db: GraphDb): CustomPluginOption {
  return {
    type: 'tooltip',
    key: 'node-tooltip',
    enable: (e: { targetType?: string; target?: { id?: unknown } }) => {
      if (e.targetType !== 'node') return false;
      const id = String(e.target?.id ?? '');
      return !id.startsWith('port:');
    },
    getContent: (_e: unknown, items: { id: string }[]) => {
      const it = items && items[0];
      if (!it) return '';
      const n = db.getNode(it.id);
      if (!n) return '';
      let rows = `<div style="color:#6b6b6b">${layerLabel(n.layer)} · ${typeLabel(n.type)}</div>`;
      if (n.domain) rows += `<div>域：${esc(n.domain)}</div>`;
      if (n.datasource) rows += `<div>数据源：${esc(n.datasource)}</div>`;
      if (n.memoryKind) rows += `<div>类别：${esc(n.memoryKind)}</div>`;
      rows += `<div>关联关系：${db.degree(n.id)} 条</div>`;
      if (isPrivateNode(n)) {
        rows += `<div style="color:#FAAD14">✦ 镜像新增 · 仅本人可见（owner：${esc(nodeOwnerId(n))}）</div>`;
      }
      if (n.definition) rows += `<div>口径：${esc(n.definition)}</div>`;
      return (
        `<div style="padding:8px 10px;max-width:260px;font-size:12px;line-height:1.55;color:#2b2b2b">` +
        `<div style="font-weight:700;margin-bottom:3px">${esc(n.label)}</div>${rows}</div>`
      );
    },
    onOpenChange: () => {},
  };
}

function edgeTooltipPlugin(db: GraphDb): CustomPluginOption {
  const endpoint = (nid: string) => {
    const n = db.getNode(nid);
    if (!n) return '?';
    return (
      `<span style="color:${LAYER_COLOR[n.layer] || '#888'};font-weight:600">${esc(n.label)}</span>` +
      `<span style="color:#8a8a8a">（${esc(layerLabel(n.layer) + ' · ' + typeLabel(n.type))}）</span>`
    );
  };
  return {
    type: 'tooltip',
    key: 'edge-tooltip',
    trigger: 'hover',
    // Ribbons / flow overlays are aggregates, not real relations — skip them
    // (ribbon clicks are handled via onRibbonClick).
    enable: (e: { targetType?: string; target?: { id?: unknown } }) => {
      if (e.targetType !== 'edge') return false;
      const id = String(e.target?.id ?? '');
      return !(id.startsWith('ribbon:') || id.startsWith('flow:'));
    },
    getContent: (
      _e: unknown,
      items: { source: string; target: string; data?: { relation?: string } }[],
    ) => {
      const it = items && items[0];
      if (!it) return '';
      const rel = (it.data && it.data.relation) || '';
      const a = db.getNode(it.source);
      const b = db.getNode(it.target);
      const cross = a && b && a.layer !== b.layer;
      let rows =
        `<div style="margin:2px 0 4px">${endpoint(it.source)}` +
        `<div style="color:#9a978d;font-size:11px;padding-left:2px;line-height:1.2">↓</div>` +
        `${endpoint(it.target)}</div>`;
      if (cross && a && b)
        rows += `<div style="color:#d68a1e">⚡ 跨层连接（${esc(a.layer + ' → ' + b.layer)}）</div>`;
      return (
        `<div style="padding:8px 10px;max-width:280px;font-size:12px;line-height:1.55;color:#2b2b2b">` +
        `<div style="font-weight:700;margin-bottom:3px">${esc(relLabel(rel))}` +
        ` <span style="font-weight:400;color:#8a8a8a;font-size:11px">${esc(rel)}</span></div>${rows}</div>`
      );
    },
    onOpenChange: () => {},
  };
}

/** Focus a node: bring to front, select, center in viewport. */
function locateInGraph(graph: Graph, id: string) {
  try {
    void graph.frontElement([id]);
  } catch {
    /* not ready */
  }
  try {
    void Promise.resolve(graph.setElementState(id, ['selected'])).catch(() => {});
  } catch {
    /* not ready */
  }
  try {
    void graph.focusElement(id, { duration: 420, easing: 'ease-in-out' });
    if (graph.getZoom() > 1.4) void graph.zoomTo(1.4);
  } catch {
    /* not ready */
  }
}

const IOS_EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
const ENTER_MS = 380;
const UPDATE_MS = 520;
const EXIT_MS = 240;
const OVERVIEW_W = 176;
const OVERVIEW_H = 112;
const OVERVIEW_PAD = 10;
const OVERVIEW_DOT = 1.6;

interface OverviewWorld {
  minX: number;
  minY: number;
  scale: number;
  ox: number;
  oy: number;
}

function elementAnimation(fields: string[]) {
  return {
    enter: [{ fields: ['opacity'], from: { opacity: 0 }, duration: ENTER_MS, easing: IOS_EASE }],
    update: [{ fields, duration: UPDATE_MS, easing: IOS_EASE }],
    exit: [{ fields: ['opacity'], to: { opacity: 0 }, duration: EXIT_MS, easing: 'ease-in' }],
  };
}

function buildG6Model(data: MultiGraphData, members: Partial<Record<LayerId, string[]>>) {
  const hasFlow = ensureFlowEdge();
  const degreeMap = new Map<string, number>();
  data.intra.concat(data.cross).forEach((e) => {
    degreeMap.set(e.source, (degreeMap.get(e.source) || 0) + 1);
    degreeMap.set(e.target, (degreeMap.get(e.target) || 0) + 1);
  });

  const g6nodes: NodeData[] = data.nodes.map((n) => {
    const deg = degreeMap.get(n.id) || 0;
    const nodeSize = sizeFor(n.layer, !!n.focus, deg);
    const c = LAYER_COLOR[n.layer];
    return {
      id: n.id,
      type: LAYER_SHAPE[n.layer] || 'circle',
      data: { label: n.label, layer: n.layer, ntype: n.type, degree: deg },
      style: {
        x: n.x,
        y: n.y,
        size: nodeSize,
        zIndex: 2,
        fill: c,
        fillOpacity: n.dim ? 0.15 : 0.8,
        stroke: n.hot ? '#2b2b2b' : n.focus ? '#c0392b' : '#ffffff',
        strokeOpacity: n.dim ? 0.25 : 1,
        lineWidth: n.hot ? 2.4 : n.focus ? 3 : 1.5,
        lineDash: n.priv ? [5, 3] : 0,
        radius: 8,
        labelText: clipLabel(n.label, nodeSize),
        labelPlacement: 'bottom',
        labelFontSize: 11,
        labelFontWeight: 500,
        labelFill: n.dim ? 'rgba(43,43,43,0.3)' : '#2b2b2b',
        labelBackground: true,
        labelBackgroundFill: 'rgba(247,246,242,0.85)',
        labelBackgroundRadius: 3,
        halo: !!(n.focus || n.hot),
        haloStroke: c,
      },
    };
  });

  data.ports.forEach((p) => {
    g6nodes.push({
      id: p.id,
      type: 'diamond',
      data: { port: true, layer: p.layer, label: '', ntype: 'port', degree: 0 },
      style: {
        x: p.x,
        y: p.y,
        size: 13,
        zIndex: 3,
        cursor: 'default',
        fill: LAYER_COLOR[p.layer],
        fillOpacity: p.dim ? 0.2 : 0.95,
        stroke: '#ffffff',
        strokeOpacity: p.dim ? 0.25 : 1,
        lineWidth: 1.4,
        shadowColor: LAYER_COLOR[p.layer],
        shadowBlur: p.dim ? 0 : 6,
      },
    });
  });

  const g6edges: EdgeData[] = [];
  data.intra.forEach((e, i) => {
    g6edges.push({
      id: 'ei' + i,
      source: e.source,
      target: e.target,
      data: { relation: e.relation, cross: false },
      type: 'line',
      style: {
        stroke: '#9a978d',
        lineWidth: 1,
        strokeOpacity: e.dim ? 0.12 : 0.5,
        cursor: 'pointer',
        zIndex: 0,
        increasedLineWidthForHitTesting: 10,
      },
    });
  });

  data.ribbons.forEach((r) => {
    const w = Math.max(3, Math.min(11, 2.4 + r.count * 0.9));
    const grad = gradient(r.colorA, r.colorB, r.sx, r.sy, r.tx, r.ty);
    g6edges.push({
      id: 'ribbon:' + r.pair,
      source: r.source,
      target: r.target,
      data: { ribbon: true, pair: r.pair },
      type: 'quadratic',
      style: {
        curveOffset: r.curveOffset || 60,
        stroke: grad,
        lineWidth: r.expanded ? w + 6 : w,
        lineCap: 'round',
        strokeOpacity: r.expanded ? 0.12 : r.dim ? 0.08 : 0.4,
        zIndex: 1,
        cursor: 'pointer',
        increasedLineWidthForHitTesting: 14,
        labelText: '×' + r.count,
        labelPlacement: 'center',
        labelFontSize: 12,
        labelFontWeight: 700,
        labelFill: r.dim ? 'rgba(43,43,43,0.35)' : '#2b2b2b',
        labelBackground: true,
        labelBackgroundFill: 'rgba(255,255,255,0.95)',
        labelBackgroundStroke: r.colorB,
        labelBackgroundStrokeOpacity: r.dim ? 0.25 : 0.7,
        labelBackgroundLineWidth: 1,
        labelBackgroundRadius: 9,
        labelPadding: [2, 7],
      },
    });
    if (hasFlow && !r.expanded && !r.dim) {
      g6edges.push({
        id: 'flow:' + r.pair,
        source: r.source,
        target: r.target,
        type: 'flow-quadratic',
        style: {
          curveOffset: r.curveOffset || 60,
          stroke: 'rgba(70,70,95,0.55)',
          lineWidth: 2.2,
          lineCap: 'round',
          lineDash: [0.1, 13],
          strokeOpacity: 0.7,
          zIndex: 1,
          pointerEvents: 'none',
        },
      });
    }
  });

  data.cross.forEach((e, i) => {
    g6edges.push({
      id: 'xc' + i,
      source: e.source,
      target: e.target,
      data: { relation: e.relation, cross: true },
      type: 'quadratic',
      style: {
        curveOffset: e.curveOffset || 0,
        stroke: gradient(e.colorA, e.colorB, e.sx, e.sy, e.tx, e.ty),
        lineWidth: 1.7,
        strokeOpacity: 0.85,
        zIndex: 2,
        cursor: 'pointer',
        increasedLineWidthForHitTesting: 10,
        endArrow: true,
        endArrowType: 'vee',
        endArrowSize: 7,
      },
    });
  });

  // `key` is always set here so the list stays assignable to updatePlugin().
  const hulls: (CustomPluginOption & { key: string })[] = [];
  LAYERS.forEach((L) => {
    const ids = members[L];
    if (!ids || !ids.length) return;
    const c = LAYER_COLOR[L];
    hulls.push({
      type: 'hull',
      key: 'hullglow-' + L,
      members: ids,
      padding: 46,
      corner: 'smooth',
      concavity: Infinity,
      fill: c,
      fillOpacity: 0.04,
      stroke: c,
      strokeOpacity: 0.1,
      lineWidth: 14,
    });
    hulls.push({
      type: 'hull',
      key: 'hull-' + L,
      members: ids,
      padding: 24,
      corner: 'smooth',
      concavity: Infinity,
      fill: c,
      fillOpacity: 0.05,
      stroke: c,
      strokeOpacity: 0.5,
      lineWidth: 1.6,
      shadowColor: c,
      shadowBlur: 10,
    });
  });

  return { g6nodes, g6edges, hulls };
}

/** G6 hull only redraws on AFTER_RENDER / AFTER_ELEMENT_UPDATE — both fire
 *  after node animations finish. Hop +/- would paint new nodes first and leave
 *  them overflowing the stale membrane. Force the contour onto the new
 *  member positions in the same draw frame as the nodes. */
type HullPlugin = {
  updateMember?: (ids: string[]) => void;
  drawHull?: () => void;
  shape?: unknown;
};

function refreshHulls(graph: Graph, members: Partial<Record<LayerId, string[]>>) {
  LAYERS.forEach((L) => {
    const ids = (members[L] || []).filter((id) => {
      try {
        return !!graph.getNodeData(id);
      } catch {
        return false;
      }
    });
    (['hullglow-', 'hull-'] as const).forEach((prefix) => {
      const inst = graph.getPluginInstance(prefix + L) as HullPlugin | undefined;
      if (!inst) return;
      try {
        if (inst.shape && inst.updateMember) inst.updateMember(ids);
        else inst.drawHull?.();
      } catch {
        try {
          inst.drawHull?.();
        } catch {
          /* hull not ready */
        }
      }
    });
  });
}

export default function MultiGraphView({
  db,
  data,
  members,
  chips,
  locateId,
  cameraNonce = 0,
  onNodeClick,
  onNodeDblClick,
  onRibbonClick,
  onGrow,
  onShrink,
  onReady,
}: MultiGraphViewProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const chipLayerRef = useRef<HTMLDivElement>(null);
  const overviewRef = useRef<HTMLDivElement>(null);
  const overviewWorldRef = useRef<OverviewWorld | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const fittedRef = useRef(false);
  const lastNonceRef = useRef(cameraNonce);
  const callbacksRef = useRef({
    onNodeClick,
    onNodeDblClick,
    onRibbonClick,
    onGrow,
    onShrink,
    onReady,
  });
  callbacksRef.current = { onNodeClick, onNodeDblClick, onRibbonClick, onGrow, onShrink, onReady };
  const locateRef = useRef(locateId);
  locateRef.current = locateId;
  const membersRef = useRef(members);
  membersRef.current = members;
  const chipsRef = useRef(chips);
  chipsRef.current = chips;
  const dbRef = useRef(db);
  dbRef.current = db;

  useEffect(() => {
    const container = containerRef.current;
    const chipLayer = chipLayerRef.current;
    if (!container || !chipLayer) return;

    while (container.firstChild) container.removeChild(container.firstChild);
    const width = container.clientWidth || 900;
    const height = container.clientHeight || 660;

    const emptyHulls: CustomPluginOption[] = [];
    LAYERS.forEach((L) => {
      const c = LAYER_COLOR[L];
      emptyHulls.push({
        type: 'hull',
        key: 'hullglow-' + L,
        members: [],
        padding: 46,
        corner: 'smooth',
        concavity: Infinity,
        fill: c,
        fillOpacity: 0.04,
        stroke: c,
        strokeOpacity: 0.1,
        lineWidth: 14,
      });
      emptyHulls.push({
        type: 'hull',
        key: 'hull-' + L,
        members: [],
        padding: 24,
        corner: 'smooth',
        concavity: Infinity,
        fill: c,
        fillOpacity: 0.05,
        stroke: c,
        strokeOpacity: 0.5,
        lineWidth: 1.6,
        shadowColor: c,
        shadowBlur: 10,
      });
    });

    const graph = new Graph({
      container,
      width,
      height,
      // Own ResizeObserver below — G6 autoResize + plugin rebuild leaves a
      // default-sized (black) canvas parked at the top-left on scope switches.
      autoResize: false,
      theme: 'light',
      padding: 80,
      data: { nodes: [], edges: [] },
      animation: true,
      node: { animation: elementAnimation(['x', 'y', 'opacity', 'fillOpacity', 'size']) },
      edge: {
        state: { active: { strokeOpacity: 0.95 } },
        animation: elementAnimation(['opacity', 'strokeOpacity', 'lineWidth']),
      },
      behaviors: ['zoom-canvas', 'drag-canvas', 'drag-element', 'hover-activate'],
      plugins: [nodeTooltipPlugin(dbRef.current), edgeTooltipPlugin(dbRef.current), ...emptyHulls],
    });

    graph.on('node:click', (e: IElementEvent) => {
      const id = String(e?.target?.id ?? '');
      if (!id || id.startsWith('port:')) return;
      callbacksRef.current.onNodeClick?.(id);
    });
    graph.on('node:dblclick', (e: IElementEvent) => {
      const id = String(e?.target?.id ?? '');
      if (!id || id.startsWith('port:')) return;
      callbacksRef.current.onNodeDblClick?.(id);
    });
    graph.on('edge:click', (e: IElementEvent) => {
      const id = String(e?.target?.id ?? '');
      if (id.startsWith('ribbon:')) callbacksRef.current.onRibbonClick?.(id.slice(7));
    });

    const chipEls: Partial<Record<LayerId, HTMLDivElement>> = {};

    function updateChips() {
      LAYERS.forEach((L) => {
        const el = chipEls[L];
        if (!el) return;
        const ids = membersRef.current[L] || [];
        let minY = Infinity;
        let minX = Infinity;
        let maxX = -Infinity;
        let ok = 0;
        ids.forEach((id) => {
          let p: Point;
          try {
            p = graph.getElementPosition(id);
          } catch {
            return;
          }
          if (!p) return;
          if (p[1] < minY) minY = p[1];
          if (p[0] < minX) minX = p[0];
          if (p[0] > maxX) maxX = p[0];
          ok++;
        });
        if (!ok) {
          el.style.display = 'none';
          return;
        }
        // Center the chip above the cluster's bounding box (MBR) so it reads
        // as the membrane's title: anchor at the bbox mid-x and hang it above
        // the topmost node (world-space offset ≈ hull padding, so it tracks
        // zoom/pan with the membrane). The old outer-corner anchoring was a
        // 2x2-grid collision workaround and read as drift once cluster counts
        // or the layout changed.
        const anchorX = (minX + maxX) / 2;
        let vp: Point;
        try {
          vp = graph.getViewportByCanvas([anchorX, minY - 46]);
        } catch {
          return;
        }
        el.style.display = '';
        el.style.left = vp[0] + 'px';
        el.style.top = vp[1] + 'px';
        el.style.transform = 'translate(-50%, -100%)';
      });
    }

    function updateOverview() {
      const host = overviewRef.current;
      const svg = host?.querySelector('svg');
      if (!host || !svg || !container) return;
      const dots: { x: number; y: number; color: string }[] = [];
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      LAYERS.forEach((L) => {
        const color = LAYER_COLOR[L];
        (membersRef.current[L] || []).forEach((id) => {
          if (id.startsWith('port:')) return;
          let p: Point;
          try {
            p = graph.getElementPosition(id);
          } catch {
            return;
          }
          if (!p) return;
          dots.push({ x: p[0], y: p[1], color });
          minX = Math.min(minX, p[0]);
          minY = Math.min(minY, p[1]);
          maxX = Math.max(maxX, p[0]);
          maxY = Math.max(maxY, p[1]);
        });
      });
      const cw = container?.clientWidth || 1;
      const ch = container?.clientHeight || 1;
      let vx0 = 0;
      let vy0 = 0;
      let vx1 = cw;
      let vy1 = ch;
      try {
        const tl = graph.getCanvasByViewport([0, 0]);
        const br = graph.getCanvasByViewport([cw, ch]);
        vx0 = Math.min(tl[0], br[0]);
        vy0 = Math.min(tl[1], br[1]);
        vx1 = Math.max(tl[0], br[0]);
        vy1 = Math.max(tl[1], br[1]);
      } catch {
        /* camera not ready */
      }
      if (!dots.length) {
        host.classList.remove(styles.gcOverviewOn);
        svg.innerHTML = '';
        return;
      }
      const pad = 36;
      minX -= pad;
      minY -= pad;
      maxX += pad;
      maxY += pad;
      const spanX = Math.max(maxX - minX, 1);
      const spanY = Math.max(maxY - minY, 1);
      const innerW = OVERVIEW_W - OVERVIEW_PAD * 2;
      const innerH = OVERVIEW_H - OVERVIEW_PAD * 2;
      const scale = Math.min(innerW / spanX, innerH / spanY);
      const ox = OVERVIEW_PAD + (innerW - spanX * scale) / 2;
      const oy = OVERVIEW_PAD + (innerH - spanY * scale) / 2;
      overviewWorldRef.current = { minX, minY, scale, ox, oy };
      const mx = (x: number) => ox + (x - minX) * scale;
      const my = (y: number) => oy + (y - minY) * scale;
      const parts = dots.map(
        (d) =>
          `<circle cx="${mx(d.x).toFixed(1)}" cy="${my(d.y).toFixed(1)}" r="${OVERVIEW_DOT}" fill="${d.color}"/>`,
      );
      const vw = Math.max(6, (vx1 - vx0) * scale);
      const vh = Math.max(6, (vy1 - vy0) * scale);
      parts.push(
        `<rect x="${mx(vx0).toFixed(1)}" y="${my(vy0).toFixed(1)}" width="${vw.toFixed(1)}" height="${vh.toFixed(1)}" fill="rgba(255,255,255,0.45)" stroke="rgba(22,119,255,0.45)" stroke-width="1"/>`,
      );
      svg.innerHTML = parts.join('');
      host.classList.add(styles.gcOverviewOn);
    }

    const refreshOverlays = () => {
      updateChips();
      updateOverview();
    };

    // Arrow (not a hoisted `function`) so the `chipLayer` non-null narrowing
    // from the guard above survives inside the closure.
    const rebuildChips = () => {
      chipLayer.innerHTML = '';
      (Object.keys(chipEls) as LayerId[]).forEach((k) => {
        delete chipEls[k];
      });
      chipsRef.current
        .filter((c) => (membersRef.current[c.layer] || []).length > 0)
        .forEach((c) => {
          const col = LAYER_COLOR[c.layer];
          const el = document.createElement('div');
          el.className = styles.gcChip;
          // Born hidden: .gcChip is absolute + translate(-50%, -100%), so an
          // unpositioned chip parks at the layer origin and its bottom border
          // plus shadow leak out of the clipped top-left corner as a 黑边.
          // updateChips() reveals it once left/top are known.
          el.style.display = 'none';
          el.style.borderColor = col;
          el.innerHTML =
            `<b style="color:${col}">${c.layer}</b><span>· ${esc(c.label)}</span>` +
            `<span class="${styles.mcCount}">${c.shown}/${c.total}</span>` +
            (c.hideHop
              ? ''
              : `<button class="${styles.mcShrink}" style="color:${col};border-color:${col}" title="折叠一跳（BFS 收回最外层）"${c.shrinkDone ? ' disabled' : ''}>−</button>` +
                `<button class="${styles.mcGrow}" style="background:${col}" title="展开下一跳（BFS 外扩一层）"${c.done ? ' disabled' : ''}>+</button>`);
          el.querySelector(`.${styles.mcGrow}`)?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            callbacksRef.current.onGrow?.(c.layer);
          });
          el.querySelector(`.${styles.mcShrink}`)?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            callbacksRef.current.onShrink?.(c.layer);
          });
          chipLayer.appendChild(el);
          chipEls[c.layer] = el;
        });
    };

    function fitNow(animate: boolean) {
      try {
        void graph.fitView(
          { when: 'always', direction: 'both' },
          animate ? { duration: UPDATE_MS, easing: IOS_EASE } : false,
        );
        if (graph.getZoom() > 1) {
          void graph.zoomTo(1, animate ? { duration: UPDATE_MS, easing: IOS_EASE } : false);
          void graph.fitCenter(animate ? { duration: UPDATE_MS, easing: IOS_EASE } : false);
        }
      } catch {
        /* canvas not sized yet */
      }
      refreshOverlays();
    }

    const onOverviewPointer = (ev: PointerEvent) => {
      ev.preventDefault();
      ev.stopPropagation();
      const world = overviewWorldRef.current;
      const host = overviewRef.current;
      if (!world || !host || world.scale <= 0) return;
      const rect = host.getBoundingClientRect();
      const cx = world.minX + (ev.clientX - rect.left - world.ox) / world.scale;
      const cy = world.minY + (ev.clientY - rect.top - world.oy) / world.scale;
      try {
        const vp = graph.getViewportByCanvas([cx, cy]);
        const dx = (container.clientWidth || 0) / 2 - vp[0];
        const dy = (container.clientHeight || 0) / 2 - vp[1];
        void graph.translateBy([dx, dy], { duration: 280, easing: IOS_EASE });
      } catch {
        /* camera not ready */
      }
    };

    graph.on(GraphEvent.AFTER_TRANSFORM, refreshOverlays);
    graph.on('node:drag', refreshOverlays);
    graph.on('node:dragend', refreshOverlays);
    // Elements exist here, animations have not started — sync membranes and
    // chip anchors now instead of waiting out the 520ms update animation.
    graph.on(GraphEvent.BEFORE_DRAW, () => {
      refreshHulls(graph, membersRef.current);
      refreshOverlays();
    });
    overviewRef.current?.addEventListener('pointerdown', onOverviewPointer);

    graphRef.current = graph;
    (
      graph as Graph & {
        __updateChips?: () => void;
        __rebuildChips?: () => void;
        __fitNow?: (a: boolean) => void;
      }
    ).__updateChips = refreshOverlays;
    (graph as Graph & { __rebuildChips?: () => void }).__rebuildChips = rebuildChips;
    (graph as Graph & { __fitNow?: (a: boolean) => void }).__fitNow = fitNow;

    void graph.render();

    let roTimer: ReturnType<typeof setTimeout> | null = null;
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        if (roTimer) clearTimeout(roTimer);
        roTimer = setTimeout(() => {
          const w = container.clientWidth;
          const h = container.clientHeight;
          if (!w || !h) return;
          try {
            graph.resize(w, h);
          } catch {
            /* destroyed mid-resize */
          }
          if (fittedRef.current) refreshOverlays();
          else fitNow(false);
        }, 120);
      });
      ro.observe(container);
    }

    return () => {
      if (roTimer) clearTimeout(roTimer);
      ro?.disconnect();
      overviewRef.current?.removeEventListener('pointerdown', onOverviewPointer);
      overviewWorldRef.current = null;
      chipLayer.innerHTML = '';
      graphRef.current = null;
      fittedRef.current = false;
      try {
        graph.destroy();
      } catch {
        /* already destroyed */
      }
    };
  }, []);

  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || !data.nodes.length) return;

    const { g6nodes, g6edges, hulls } = buildG6Model(data, members);
    try {
      graph.setData({ nodes: g6nodes, edges: g6edges });
      // updatePlugin keeps the same canvas layers. setPlugins() tears them
      // down and G6 briefly inserts a default 300×150 surface (black clear
      // color) at (0, 0) — the top-left "黑边" on 视角/数据源 switches.
      hulls.forEach((hull) => {
        try {
          graph.updatePlugin(hull);
        } catch {
          /* key not ready yet */
        }
      });
      LAYERS.forEach((L) => {
        if (members[L]?.length) return;
        try {
          graph.updatePlugin({ key: 'hullglow-' + L, members: [] });
          graph.updatePlugin({ key: 'hull-' + L, members: [] });
        } catch {
          /* ignore */
        }
      });
      // Same-frame membrane update: updatePlugin only writes options;
      // G6 hull otherwise waits until node animations finish.
      refreshHulls(graph, members);
    } catch {
      return;
    }

    const helper = graph as Graph & {
      __updateChips?: () => void;
      __rebuildChips?: () => void;
      __fitNow?: (a: boolean) => void;
    };
    helper.__rebuildChips?.();

    const shouldFit = !fittedRef.current || lastNonceRef.current !== cameraNonce;
    lastNonceRef.current = cameraNonce;
    const firstPaint = !fittedRef.current;

    const after = () => {
      if (shouldFit) {
        helper.__fitNow?.(!firstPaint);
        fittedRef.current = true;
        if (locateRef.current) locateInGraph(graph, locateRef.current);
        callbacksRef.current.onReady?.({
          fit: () => helper.__fitNow?.(false),
          exportPNG: (filename) => {
            Promise.resolve(graph.toDataURL({ type: 'image/png', encoderOptions: 0.95 }))
              .then((url) => {
                const a = document.createElement('a');
                a.download = filename || 'cm-multigraph.png';
                a.href = url;
                a.click();
              })
              .catch(() => {
                /* canvas export unsupported */
              });
          },
        });
      } else {
        helper.__updateChips?.();
      }
    };

    const run = firstPaint ? graph.render() : graph.draw();
    void Promise.resolve(run)
      .then(() => {
        const host = containerRef.current;
        if (host) {
          host.querySelectorAll('canvas').forEach((c) => {
            const w = Number(c.getAttribute('width') || c.width || 0);
            const h = Number(c.getAttribute('height') || c.height || 0);
            if (w > 0 && h > 0 && w <= 300 && h <= 150 && host.clientWidth > 400) {
              c.remove();
            }
          });
        }
        after();
      })
      .catch(after);
  }, [db, data, members, chips, cameraNonce]);

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div ref={containerRef} className={styles.gcCanvas} />
      <div ref={chipLayerRef} className={styles.gcChipLayer} />
      <div
        ref={overviewRef}
        className={styles.gcOverview}
        title={t('graphCenter.canvasOverview')}
        aria-label={t('graphCenter.canvasOverview')}
      >
        <svg
          className={styles.gcOverviewSvg}
          width={OVERVIEW_W}
          height={OVERVIEW_H}
          viewBox={`0 0 ${OVERVIEW_W} ${OVERVIEW_H}`}
        />
      </div>
    </div>
  );
}
