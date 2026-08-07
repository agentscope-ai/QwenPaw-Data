import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Graph } from "@antv/g6";
import type { EdgeData, IElementEvent, NodeData } from "@antv/g6";
import type { GraphNode, GraphEdge } from "../types";
import { getNodeColor } from "../mock-data";
import { BRAND_PRIMARY } from "@/brand";
import styles from "../index.module.less";

export interface GraphCanvasHandle {
  toDataURL: () => Promise<string>;
  zoomIn: () => Promise<void>;
  zoomOut: () => Promise<void>;
  fitView: () => Promise<void>;
  resize: () => Promise<void>;
}

interface GraphCanvasProps {
  graphNodes: GraphNode[];
  graphEdges: GraphEdge[];
  onNodeClick?: (nodeId: string) => void;
  onEdgeClick?: (edgeId: string) => void;
  onPaneClick?: () => void;
  selectedEdgeId?: string | null;
}

const GRAPH_VIEWPORT_ANIMATION = { duration: 220, easing: "ease-in-out" };
const ZOOM_STEP = 1.2;
const MIN_LABEL_CHARS = 12;
const MAX_LABEL_CHARS = 24;

function getNodeSize(degree: number) {
  return Math.max(32, Math.min(56, 32 + degree * 3));
}

function truncateMiddle(text: string, maxChars: number) {
  if (text.length <= maxChars) return text;
  const headLength = Math.max(4, Math.floor((maxChars - 1) * 0.45));
  const tailLength = Math.max(4, maxChars - headLength - 1);
  return `${text.slice(0, headLength)}…${text.slice(-tailLength)}`;
}

function compactNodeLabel(label: string, nodeSize: number) {
  const maxChars = Math.max(
    MIN_LABEL_CHARS,
    Math.min(MAX_LABEL_CHARS, Math.floor(nodeSize / 3)),
  );
  if (label.length <= maxChars) return label;

  const segments = label.split(":").filter(Boolean);
  if (segments.length >= 3) {
    const tail = segments.slice(-2).join(":");
    if (tail.length <= maxChars) return tail;
    return truncateMiddle(tail, maxChars);
  }

  return truncateMiddle(label, maxChars);
}

function appendTooltipRow(root: HTMLElement, label: string, value: string) {
  const row = document.createElement("div");
  row.className = styles.graphTooltipRow;

  const key = document.createElement("span");
  key.className = styles.graphTooltipKey;
  key.textContent = label;

  const text = document.createElement("span");
  text.className = styles.graphTooltipValue;
  text.textContent = value || "-";

  row.appendChild(key);
  row.appendChild(text);
  root.appendChild(row);
}

type TooltipDatum = {
  id?: string;
  source?: string;
  target?: string;
  data?: { label?: string; type?: string };
};

function createTooltipContent(item: unknown) {
  const root = document.createElement("div");
  root.className = styles.graphTooltip;

  const datum = (item ?? {}) as TooltipDatum;
  const data = datum.data ?? {};
  const isEdge = "source" in datum || "target" in datum;
  const title = document.createElement("div");
  title.className = styles.graphTooltipTitle;
  title.textContent = isEdge
    ? data.label || datum.id || "Relationship"
    : data.label || datum.id || "Node";
  root.appendChild(title);

  if (isEdge) {
    appendTooltipRow(root, "source", String(datum.source ?? ""));
    appendTooltipRow(root, "target", String(datum.target ?? ""));
  } else {
    appendTooltipRow(root, "type", String(data.type ?? ""));
    appendTooltipRow(root, "id", String(datum.id ?? ""));
  }

  return root;
}

const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(function GraphCanvas({
  graphNodes,
  graphEdges,
  onNodeClick,
  onEdgeClick,
  onPaneClick,
  selectedEdgeId,
}, ref) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  const renderedGraphRef = useRef<Graph | null>(null);

  useImperativeHandle(ref, () => ({
    toDataURL: () => {
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return Promise.reject(new Error("Graph not ready"));
      return graph.toDataURL();
    },
    zoomIn: () => {
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return Promise.reject(new Error("Graph not ready"));
      return graph.zoomBy(ZOOM_STEP, GRAPH_VIEWPORT_ANIMATION);
    },
    zoomOut: () => {
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return Promise.reject(new Error("Graph not ready"));
      return graph.zoomBy(1 / ZOOM_STEP, GRAPH_VIEWPORT_ANIMATION);
    },
    fitView: () => {
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return Promise.reject(new Error("Graph not ready"));
      return graph.fitView({ when: "always", direction: "both" }, GRAPH_VIEWPORT_ANIMATION);
    },
    resize: async () => {
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return Promise.reject(new Error("Graph not ready"));
      graph.resize();
      await graph.fitView({ when: "always", direction: "both" }, GRAPH_VIEWPORT_ANIMATION);
    },
  }));

  const callbacksRef = useRef({ onNodeClick, onEdgeClick, onPaneClick });
  callbacksRef.current = { onNodeClick, onEdgeClick, onPaneClick };
  const selectedEdgeIdRef = useRef<string | null | undefined>(selectedEdgeId);
  selectedEdgeIdRef.current = selectedEdgeId;

  const applyEdgeSelection = () => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed || renderedGraphRef.current !== graph) return;
    try {
      const states: Record<string, string[]> = {};
      graphEdges.forEach((e) => {
        states[e.id] =
          selectedEdgeIdRef.current && e.id === selectedEdgeIdRef.current
            ? ["selected"]
            : [];
      });
      void Promise.resolve(graph.setElementState(states, false)).catch(() => {});
    } catch {
      // Graph elements may not be ready yet; the selection effect will retry.
    }
  };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (graphNodes.length === 0 && graphEdges.length === 0) return;

    // ── Compute node degrees for dynamic sizing ──
    const degreeMap = new Map<string, number>();
    graphEdges.forEach((e) => {
      degreeMap.set(e.source, (degreeMap.get(e.source) || 0) + 1);
      degreeMap.set(e.target, (degreeMap.get(e.target) || 0) + 1);
    });

    const nodes = graphNodes.map((n) => ({
      id: n.id,
      data: {
        label: n.label,
        type: n.type,
        color: getNodeColor(n.type),
        properties: n.properties,
        degree: degreeMap.get(n.id) || 0,
      },
    }));

    // ── Detect parallel edges and assign curve offsets ──
    const groupKey = (s: string, t: string) => [s, t].sort().join("\0");
    const groupTotals = new Map<string, number>();
    graphEdges.forEach((e) => {
      const k = groupKey(e.source, e.target);
      groupTotals.set(k, (groupTotals.get(k) || 0) + 1);
    });
    const groupCounters = new Map<string, number>();

    const edges = graphEdges.map((e) => {
      const k = groupKey(e.source, e.target);
      const total = groupTotals.get(k) || 1;
      const idx = groupCounters.get(k) || 0;
      groupCounters.set(k, idx + 1);
      const curveOffset =
        total > 1 ? (idx - (total - 1) / 2) * 30 : 0;
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        data: { label: e.type, curveOffset },
      };
    });

    // Destroy previous instance
    if (graphRef.current) {
      graphRef.current.destroy();
      graphRef.current = null;
    }

    let disposed = false;
    let mountedGraph: Graph | null = null;
    const animationFrame = window.requestAnimationFrame(() => {
      if (disposed) return;

      const graph = new Graph({
      container,
      autoFit: {
        type: "view",
        options: { when: "always", direction: "both" },
      },
      padding: 60,
      data: { nodes, edges },
      node: {
        type: "circle",
        style: {
          size: (d: NodeData) => {
            const degree = (d.data?.degree as number) || 0;
            return getNodeSize(degree);
          },
          fill: (d: NodeData) => (d.data?.color as string) || "#78909c",
          stroke: "#fff",
          lineWidth: 2,
          labelText: (d: NodeData) => {
            const label = (d.data?.label as string) || "";
            const degree = (d.data?.degree as number) || 0;
            return compactNodeLabel(label, getNodeSize(degree));
          },
          labelFill: "#fff",
          labelFontSize: 10,
          labelFontWeight: 500,
          labelPlacement: "center",
          shadowColor: "rgba(0,0,0,0.15)",
          shadowBlur: 8,
          shadowOffsetY: 2,
          cursor: "pointer",
        },
        state: {
          selected: {
            stroke: "#0D76FD",
            lineWidth: 3,
          },
          hover: {
            stroke: "#0D76FD",
            lineWidth: 2,
            shadowColor: "rgba(13, 118, 253,0.4)",
            shadowBlur: 12,
          },
        },
      },
      edge: {
        type: "quadratic",
        style: {
          stroke: "#b0b8c0",
          lineWidth: 1,
          endArrow: true,
          curveOffset: (d: EdgeData) => (d.data?.curveOffset as number) || 0,
          labelText: (d: EdgeData) => (d.data?.label as string) || "",
          labelFontSize: 10,
          labelFill: "#888",
          labelBackground: true,
          labelBackgroundFill: "rgba(255,255,255,0.85)",
          labelBackgroundRadius: 2,
          cursor: "pointer",
        },
        state: {
          selected: {
            stroke: "#f0c040",
            lineWidth: 2,
          },
        },
      },
      layout: {
        type: "d3-force",
        manyBody: { strength: -200 },
        link: { distance: 150 },
        collide: { radius: 40 },
        center: { strength: 0.05 },
      },
      behaviors: ["zoom-canvas", "drag-canvas", "drag-element"],
      plugins: [
        {
          type: "tooltip",
          key: "full-label-tooltip",
          trigger: "hover",
          enable: (_event: unknown, items: unknown[]) => items.length > 0,
          getContent: (_event: unknown, items: unknown[]) => createTooltipContent(items[0]),
          onOpenChange: () => {},
        },
        {
          type: "minimap",
          key: "viewport-minimap",
          size: [160, 104],
          position: "right-bottom",
          padding: 8,
          containerStyle: {
            border: "1px solid rgba(0, 0, 0, 0.12)",
            borderRadius: "6px",
            background: "rgba(255, 255, 255, 0.92)",
            boxShadow: "0 2px 8px rgba(0, 0, 0, 0.08)",
            overflow: "hidden",
          },
          maskStyle: {
            border: `1px solid ${BRAND_PRIMARY}`,
            background: "rgba(13, 118, 253, 0.12)",
          },
        },
      ],
      animation: true,
    });

      mountedGraph = graph;
      graphRef.current = graph;

      graph.on("node:click", (evt: IElementEvent) => {
        const nodeId = evt.target?.id;
        if (nodeId) callbacksRef.current.onNodeClick?.(nodeId);
      });

      graph.on("edge:click", (evt: IElementEvent) => {
        const edgeId = evt.target?.id;
        if (edgeId) callbacksRef.current.onEdgeClick?.(edgeId);
      });

      graph.on("canvas:click", () => {
        callbacksRef.current.onPaneClick?.();
      });

      Promise.resolve(graph.render())
        .then(() => {
          if (disposed || graph.destroyed || graphRef.current !== graph) return;
          renderedGraphRef.current = graph;
          applyEdgeSelection();
        })
        .catch(() => {
          // A render can be superseded by new graph data or React cleanup.
        });
    });

    return () => {
      disposed = true;
      window.cancelAnimationFrame(animationFrame);
      if (mountedGraph) {
        if (renderedGraphRef.current === mountedGraph) renderedGraphRef.current = null;
        mountedGraph.destroy();
        if (graphRef.current === mountedGraph) graphRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphNodes, graphEdges]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;

    let animationFrame = 0;
    const resizeObserver = new ResizeObserver(() => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        const graph = graphRef.current;
        if (!graph || graph.destroyed) return;
        graph.resize();
        graph
          .fitView({ when: "always", direction: "both" }, GRAPH_VIEWPORT_ANIMATION)
          .catch(() => {});
      });
    });

    resizeObserver.observe(container);
    return () => {
      resizeObserver.disconnect();
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
    };
  }, []);

  useEffect(() => {
    applyEdgeSelection();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedEdgeId, graphEdges]);

  if (!graphNodes.length && !graphEdges.length) {
    return <div className={styles.graphEmpty}>{t("kgBrowser.noGraphData")}</div>;
  }

  return (
    <div
      ref={containerRef}
      style={{ position: "absolute", inset: 0, overflow: "hidden" }}
    />
  );
});

export default GraphCanvas;
