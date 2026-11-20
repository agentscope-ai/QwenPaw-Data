import { useEffect, useRef } from 'react';
import { Graph, GraphEvent } from '@antv/g6';
import type { IElementEvent, NodeData, EdgeData } from '@antv/g6';
import type { SubgraphData, LayerId, NodeType } from '../types';
import { LAYER_SHAPE, LAYER_COLOR, relLabel } from '../dataset/constants';
import {
  isPrivateNode,
  nodeOwnerId,
  typeLabel,
  layerLabel,
  type GraphDb,
} from '../dataset/graphDb';

export type SubgraphLayout = 'layered' | 'force';

interface GraphViewProps {
  /** Query API over the currently loaded dataset (real scope or demo). */
  db: GraphDb;
  data: SubgraphData;
  focusId?: string | null;
  layout?: SubgraphLayout;
  dark?: boolean;
  /** CSS height for the canvas container (defaults to 420px). */
  height?: number;
  /** Optional CSS width; defaults to container width. */
  width?: number;
  onNodeClick?: (nodeId: string) => void;
}

const MIN_LABEL_CHARS = 12;
const MAX_LABEL_CHARS = 24;

/** Dynamic node size by degree — mirrors cm-graph-demo/js/graph.js sizeFor. */
function sizeFor(layer: LayerId, focus: boolean, deg: number): number | [number, number] {
  let base = Math.max(32, Math.min(56, 32 + (deg || 0) * 3));
  if (layer === 'KG') base = base + 16; // rect: wider
  if (focus) return Array.isArray(base) ? [base[0] + 10, base[1] + 8] : base + 8;
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

/** Node tooltip — same content as the demo (layer/type/domain/degree/private MG…). */
function nodeTooltipPlugin(db: GraphDb) {
  return {
    type: 'tooltip',
    key: 'node-tooltip',
    enable: (e: { targetType?: string }) => e.targetType === 'node',
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

/** Edge tooltip — hover shows relation label + endpoints; cross-layer flagged. */
function edgeTooltipPlugin(db: GraphDb) {
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
    enable: (e: { targetType?: string }) => e.targetType === 'edge',
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

/** Small directed subgraph canvas (detail-page accordions + audit drawer).
 *  Direct port of Graph.render() from cm-graph-demo/js/graph.js. */
export default function GraphView({
  db,
  data,
  focusId,
  layout = 'layered',
  dark = false,
  height = 420,
  width,
  onNodeClick,
}: GraphViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const callbacksRef = useRef({ onNodeClick });
  callbacksRef.current = { onNodeClick };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (!data.nodes.length) return;

    // Stale-canvas guard (same as MultiGraphView): G6's destroy() may not
    // remove its canvases when it dies mid-render-loop; clear leftovers so
    // the previous render never shows through.
    while (container.firstChild) container.removeChild(container.firstChild);

    const byId = new Map(data.nodes.map((n) => [n.id, n]));
    const degreeMap = new Map<string, number>();
    // The demo's traverse can emit edges whose endpoint fell off the node cap;
    // the npm G6 throws on dangling edge endpoints, so filter them out.
    const safeEdges = data.edges.filter((e) => byId.has(e.source) && byId.has(e.target));
    safeEdges.forEach((e) => {
      degreeMap.set(e.source, (degreeMap.get(e.source) || 0) + 1);
      degreeMap.set(e.target, (degreeMap.get(e.target) || 0) + 1);
    });

    const nodes: NodeData[] = data.nodes.map((n) => {
      const focus = n.id === focusId;
      const deg = degreeMap.get(n.id) || 0;
      const nodeSize = sizeFor(n.layer, focus, deg);
      const sizeNum = Array.isArray(nodeSize) ? nodeSize[0] : nodeSize;
      return {
        id: n.id,
        type: LAYER_SHAPE[n.layer] || 'circle',
        data: { label: n.label, layer: n.layer, ntype: n.type as NodeType, degree: deg },
        style: {
          size: nodeSize,
          fill: LAYER_COLOR[n.layer],
          fillOpacity: 0.95,
          stroke: focus ? '#c0392b' : '#ffffff',
          lineWidth: focus ? 3 : 1.5,
          radius: 8,
          labelText: clipLabel(n.label, sizeNum),
          labelPlacement: 'bottom' as const,
          labelFontSize: 10,
          labelFontWeight: 500,
          labelFill: dark ? '#dfe4f5' : '#2b2b2b',
          labelBackground: dark,
          labelBackgroundFill: dark ? 'rgba(10,14,39,0.55)' : 'transparent',
          labelBackgroundRadius: 3,
          halo: focus,
          haloStroke: LAYER_COLOR[n.layer],
        },
      };
    });

    const edges: EdgeData[] = safeEdges.map((e, i) => {
      const sl = byId.get(e.source)?.layer;
      const tl = byId.get(e.target)?.layer;
      return {
        id: 'e' + i,
        source: e.source,
        target: e.target,
        data: { cross: !!(sl && tl && sl !== tl), relation: e.relation },
      };
    });

    const graph = new Graph({
      container,
      width: width || container.clientWidth || 760,
      height,
      theme: dark ? 'dark' : 'light',
      data: { nodes, edges },
      node: {
        state: {
          selected: { stroke: '#0D76FD', lineWidth: 3 },
          hover: {
            stroke: '#0D76FD',
            lineWidth: 2,
            shadowColor: 'rgba(13,118,253,0.4)',
            shadowBlur: 12,
          },
        },
      },
      edge: {
        type: 'cubic-horizontal',
        style: {
          endArrow: true,
          endArrowType: 'vee',
          stroke: dark ? '#5b6690' : '#9a978d',
          lineWidth: 1.4,
          strokeOpacity: 0.8,
          cursor: 'pointer',
          increasedLineWidthForHitTesting: 10,
          lineDash: (d: { data?: { cross?: boolean } }) => (d.data && d.data.cross ? [6, 4] : 0),
        },
      },
      behaviors: ['zoom-canvas', 'drag-canvas', 'drag-element', 'hover-activate'],
      plugins: [nodeTooltipPlugin(db), edgeTooltipPlugin(db)],
      layout:
        layout === 'force'
          ? {
              type: 'd3-force',
              manyBody: { strength: -200 },
              link: { distance: 150 },
              collide: { radius: 40 },
              center: { strength: 0.05 },
            }
          : { type: 'dagre', rankdir: 'LR', ranksep: 70, nodesep: 24, nodeSize: [70, 60] },
    });

    graph.on('node:click', (e: IElementEvent) => {
      const id = e?.target?.id;
      if (id) callbacksRef.current.onNodeClick?.(String(id));
    });

    graph.on(GraphEvent.AFTER_LAYOUT, () => {
      try {
        void graph.fitView({ when: 'always', direction: 'both' });
        if (graph.getZoom() > 1.2) void graph.zoomTo(1.2);
      } catch {
        // fit can fail when the canvas is hidden; non-fatal.
      }
    });

    graph.render();

    return () => {
      try {
        graph.destroy();
      } catch {
        // already destroyed
      }
    };
  }, [db, data, focusId, layout, dark, height, width]);

  return (
    <div
      ref={containerRef}
      style={{ width: width ? width : '100%', height, position: 'relative', overflow: 'hidden' }}
    />
  );
}
