import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Tooltip, Input, message } from "@/design";
import {
  ExpandOutlined,
  CompressOutlined,
  CloseOutlined,
  DownloadOutlined,
  CaretRightOutlined,
  WarningOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
  AimOutlined,
} from "@/design";
import { useTranslation } from "react-i18next";
import GraphCanvas from "./GraphCanvas";
import type { GraphCanvasHandle } from "./GraphCanvas";
import OverviewPanel from "./OverviewPanel";
import NodePropertiesPanel from "./NodePropertiesPanel";
import RelationshipPropertiesPanel from "./RelationshipPropertiesPanel";
import TableView from "./TableView";
import TextView from "./TextView";
import CodeView from "./CodeView";
import { NODE_TYPE_COLORS } from "../mock-data";
import type { QueryFrame, ViewTab } from "../types";
import styles from "../index.module.less";

const ALL_VIEW_TABS: { key: ViewTab; icon: string }[] = [
  { key: "graph", icon: "⚡" },
  { key: "table", icon: "☰" },
  { key: "text", icon: "A" },
  { key: "code", icon: "{}" },
];

interface ResultFrameProps {
  frame: QueryFrame;
  onTabChange: (frameId: string, tab: ViewTab) => void;
  onClose: (frameId: string) => void;
  onNodeUpdate?: (frameId: string, nodeId: string, properties: Record<string, unknown>) => void;
  onEdgeUpdate?: (frameId: string, edgeId: string, properties: Record<string, unknown>) => void;
  onExecuteQuery?: (query: string, frameId?: string) => void;
  errorMessage?: string;
  onDismissError?: (frameId: string) => void;
  loading?: boolean;
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function escapeCsvValue(value: unknown): string {
  const str = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  if (str.includes('"') || str.includes(",") || str.includes("\n")) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

export default function ResultFrame({
  frame,
  onTabChange,
  onClose,
  onNodeUpdate,
  onEdgeUpdate,
  onExecuteQuery,
  errorMessage,
  onDismissError,
  loading,
}: ResultFrameProps) {
  const { t } = useTranslation();

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [editQuery, setEditQuery] = useState(frame.query);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const frameRef = useRef<HTMLDivElement>(null);
  const graphCanvasRef = useRef<GraphCanvasHandle>(null);
  const resizeGraphCanvas = useCallback(() => {
    const resize = () => graphCanvasRef.current?.resize().catch(() => {});
    window.setTimeout(resize, 80);
    window.setTimeout(resize, 240);
  }, []);

  // Sync editQuery when frame.query changes (e.g. new search results replace the frame)
  useEffect(() => {
    setEditQuery(frame.query);
  }, [frame.query]);

  const nodeById = useMemo(() => {
    return new Map(frame.result.nodes.map((n) => [n.id, n] as const));
  }, [frame.result.nodes]);

  const edgeById = useMemo(() => {
    return new Map(frame.result.edges.map((e) => [e.id, e] as const));
  }, [frame.result.edges]);

  const selectedNode = selectedNodeId ? nodeById.get(selectedNodeId) ?? null : null;
  const selectedEdge = selectedEdgeId ? edgeById.get(selectedEdgeId) ?? null : null;

  // Dynamically filter tabs: hide "graph" when there is no graph data
  const hasGraphData = frame.result.nodes.length > 0 || frame.result.edges.length > 0;
  const viewTabs = useMemo(
    () => ALL_VIEW_TABS.filter((tab) => tab.key !== "graph" || hasGraphData),
    [hasGraphData],
  );

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
  }, []);

  const handleEdgeClick = useCallback((edgeId: string) => {
    setSelectedEdgeId(edgeId);
    setSelectedNodeId(null);
  }, []);

  const handlePaneClick = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  }, []);

  const handleExecute = useCallback(() => {
    onExecuteQuery?.(editQuery, frame.id);
  }, [editQuery, onExecuteQuery, frame.id]);

  const runGraphControl = useCallback((action: keyof Pick<GraphCanvasHandle, "zoomIn" | "zoomOut" | "fitView">) => {
    const actionPromise = graphCanvasRef.current?.[action]();
    actionPromise?.catch(() => {
      message.error(t("kgBrowser.genericError"));
    });
  }, [t]);

  useEffect(() => {
    const onFullscreenChange = () => {
      setIsFullscreen(document.fullscreenElement === frameRef.current);
      resizeGraphCanvas();
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, [resizeGraphCanvas]);

  const handleToggleFullscreen = useCallback(async () => {
    if (!frameRef.current) return;
    try {
      if (document.fullscreenElement === frameRef.current) {
        await document.exitFullscreen();
      } else {
        await frameRef.current.requestFullscreen();
      }
      resizeGraphCanvas();
    } catch {
      message.error(t("kgBrowser.genericError"));
    }
  }, [resizeGraphCanvas, t]);

  const handleDownload = useCallback(async () => {
    const ts = Date.now();
    try {
      switch (frame.activeTab) {
        case "graph": {
          if (!graphCanvasRef.current) return;
          const dataUrl = await graphCanvasRef.current.toDataURL();
          const res = await fetch(dataUrl);
          const blob = await res.blob();
          downloadBlob(blob, `cmgraph-graph-${ts}.png`);
          break;
        }
        case "table": {
          const cols = frame.columns ?? [];
          const rows = frame.rows ?? [];
          const header = cols.map(escapeCsvValue).join(",");
          const body = rows.map((row) => cols.map((col) => escapeCsvValue(row[col])).join(",")).join("\n");
          const csv = header + "\n" + body;
          downloadBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), `cmgraph-table-${ts}.csv`);
          break;
        }
        case "text": {
          const rows = frame.rows ?? [];
          const text = rows
            .map((row) =>
              Object.entries(row)
                .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
                .join(", "),
            )
            .join("\n");
          downloadBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), `cmgraph-text-${ts}.txt`);
          break;
        }
        case "code": {
          const json = JSON.stringify(frame.meta ?? {}, null, 2);
          downloadBlob(new Blob([json], { type: "application/json;charset=utf-8" }), `cmgraph-code-${ts}.json`);
          break;
        }
      }
    } catch {
      message.error("Download failed");
    }
  }, [frame.activeTab, frame.columns, frame.rows, frame.meta]);

  return (
    <div className={styles.resultFrame} ref={frameRef}>
      {/* Frame header: editable query input + toolbar */}
      <div className={styles.frameHeader}>
        <div className={styles.frameQuery}>
          <span className={styles.frameQueryPrefix}>$</span>
          <Input
            value={editQuery}
            onChange={(e) => setEditQuery(e.target.value)}
            onPressEnter={handleExecute}
            className={styles.frameQueryInput}
            variant="borderless"
            size="small"
          />
          {frame.subTitle && (
            <span className={styles.frameQuerySub}>{frame.subTitle}</span>
          )}
        </div>
        <div className={styles.frameActions}>
          <Button
            type="primary"
            icon={<CaretRightOutlined />}
            size="small"
            onClick={handleExecute}
            loading={loading}
            disabled={loading}
            className={styles.frameExecuteBtn}
          />
          <Tooltip title={t("kgBrowser.download")}>
            <Button type="text" icon={<DownloadOutlined />} size="small" onClick={handleDownload} />
          </Tooltip>
          <Tooltip title={isFullscreen ? t("kgBrowser.exitFullscreen") : t("kgBrowser.fullscreen")}>
            <Button type="text" icon={isFullscreen ? <CompressOutlined /> : <ExpandOutlined />} size="small" onClick={handleToggleFullscreen} />
          </Tooltip>
          <Tooltip title={t("kgBrowser.close")}>
            <Button
              type="text"
              icon={<CloseOutlined />}
              size="small"
              onClick={() => onClose(frame.id)}
            />
          </Tooltip>
        </div>
      </div>

      {/* Inline error panel — shown below query bar when execution fails */}
      {errorMessage && (
        <div className={styles.frameErrorPanel}>
          <WarningOutlined className={styles.frameErrorIcon} />
          <span className={styles.frameErrorText}>{errorMessage}</span>
          <CloseOutlined className={styles.frameErrorClose} onClick={() => onDismissError?.(frame.id)} />
        </div>
      )}

      {/* Frame body: tabs + content */}
      <div className={styles.frameBody}>
        {/* Left vertical tabs */}
        <div className={styles.frameTabs}>
          {viewTabs.map((tab) => (
            <div
              key={tab.key}
              className={`${styles.frameTab} ${frame.activeTab === tab.key ? styles.frameTabActive : ""}`}
              onClick={() => onTabChange(frame.id, tab.key)}
            >
              <span className={styles.frameTabIcon}>{tab.icon}</span>
              <span className={styles.frameTabLabel}>
                {t(`kgBrowser.${tab.key}View`)}
              </span>
            </div>
          ))}
        </div>

        {/* Content area */}
        <div className={styles.frameContent}>
          {frame.activeTab === "graph" && (
            <>
              <div style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}>
                <GraphCanvas
                  ref={graphCanvasRef}
                  graphNodes={frame.result.nodes}
                  graphEdges={frame.result.edges}
                  onNodeClick={handleNodeClick}
                  onEdgeClick={handleEdgeClick}
                  onPaneClick={handlePaneClick}
                  selectedEdgeId={selectedEdgeId}
                />
              </div>
              <div className={styles.graphViewportControls}>
                <Tooltip title={t("kgBrowser.zoomIn")}>
                  <Button
                    type="text"
                    size="small"
                    icon={<ZoomInOutlined />}
                    onClick={() => runGraphControl("zoomIn")}
                  />
                </Tooltip>
                <Tooltip title={t("kgBrowser.zoomOut")}>
                  <Button
                    type="text"
                    size="small"
                    icon={<ZoomOutOutlined />}
                    onClick={() => runGraphControl("zoomOut")}
                  />
                </Tooltip>
                <Tooltip title={t("kgBrowser.fitView")}>
                  <Button
                    type="text"
                    size="small"
                    icon={<AimOutlined />}
                    onClick={() => runGraphControl("fitView")}
                  />
                </Tooltip>
              </div>
              {selectedNode ? (
                <NodePropertiesPanel
                  node={selectedNode}
                  nodeColor={NODE_TYPE_COLORS[selectedNode.type] || "#78909c"}
                  onClose={handlePaneClick}
                  onNodeUpdate={(nodeId, properties) =>
                    onNodeUpdate?.(frame.id, nodeId, properties)
                  }
                />
              ) : selectedEdge ? (
                <RelationshipPropertiesPanel
                  edge={selectedEdge}
                  onClose={handlePaneClick}
                  onEdgeUpdate={(edgeId, properties) =>
                    onEdgeUpdate?.(frame.id, edgeId, properties)
                  }
                />
              ) : (
                <OverviewPanel frame={frame} />
              )}
            </>
          )}
          {frame.activeTab === "table" && <TableView frame={frame} />}
          {frame.activeTab === "text" && <TextView frame={frame} />}
          {frame.activeTab === "code" && <CodeView frame={frame} />}
        </div>
      </div>
    </div>
  );
}
