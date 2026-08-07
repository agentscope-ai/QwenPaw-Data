import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Empty, Button, Input, Tooltip, Spin, message } from "@/design";
import { ReloadOutlined, CaretRightOutlined, WarningOutlined, CloseOutlined } from "@/design";
import DatabaseSidebar from "./components/DatabaseSidebar";
import type { SubgraphSearchResult } from "./components/DatabaseSidebar";
import ResultFrame from "./components/ResultFrame";
import { cypherApi, explorerApi, apiNodeToGraphNode, apiEdgeToGraphEdge } from "@/services/cmgraph";
import type { QueryFrame, ViewTab, GraphZone, ZoneTabConfig } from "./types";
import { splitGraphIntoComponents, buildRowsFromNodes } from "./utils/graphUtils";
import styles from "./index.module.less";

/** Zone tab configuration — editable / readonly ranges per layer.
 *  KG: all node & edge properties editable.
 *  TG: Claim / Strategy Card editable; Task / Step / ToolCall / User read-only.
 *  MG: everything read-only. */
const ZONE_TABS: ZoneTabConfig[] = [
  {
    key: "kg",
    zoneMode: "knowledge",
    editableLabels: ["Entity", "Event"],
    readonlyLabels: [],
    allEdgesEditable: true,
  },
  {
    key: "tg",
    zoneMode: "trace",
    editableLabels: ["Claim", "Strategy", "StrategyCard"],
    readonlyLabels: ["Task", "Step", "ToolCall", "User"],
    allEdgesEditable: false,
  },
  {
    key: "mg",
    zoneMode: "metadata",
    editableLabels: [],
    readonlyLabels: ["Domain", "Metric", "Dimension", "Dataset"],
    allEdgesEditable: false,
  },
];

/** Default Cypher query executed on initial load. */
const DEFAULT_QUERY = "MATCH (n) RETURN n LIMIT 50";
const REQUEST_TIMEOUT_MS = 10000;

/** Map API zone mode strings to GraphZone tab keys. */
const ZONE_MODE_TO_KEY: Record<string, GraphZone> = {
  knowledge: "kg",
  trace: "tg",
  metadata: "mg",
};

type ZoneCache = Record<string, QueryFrame[] | null>;

function getZoneCacheKey(zone: GraphZone, datasourceCode?: string): string {
  return zone === "kg" ? zone : `${zone}:${datasourceCode?.trim() || "__unselected__"}`;
}

function getDatasourceCodeForZone(
  zone: GraphZone,
  datasourceCode?: string,
): string | undefined {
  if (zone === "kg") return undefined;
  return datasourceCode?.trim() || undefined;
}

const createEmptyZoneCache = (): ZoneCache => ({ kg: null });

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      window.setTimeout(() => reject(new Error("Request timeout")), timeoutMs);
    }),
  ]);
}

export default function CMGraphPage() {
  const { t } = useTranslation();

  const [frames, setFrames] = useState<QueryFrame[]>([]);
  const [pageLoading, setPageLoading] = useState(false);
  const [frameLoadingIds, setFrameLoadingIds] = useState<Record<string, boolean>>({});
  const [cypherError, setCypherError] = useState<string | null>(null);
  const [frameErrors, setFrameErrors] = useState<Record<string, string>>({});
  const [selectedDatasourceCode, setSelectedDatasourceCode] = useState<string>();
  const [initialQuery, setInitialQuery] = useState(DEFAULT_QUERY);
  const [activeZone, setActiveZone] = useState<GraphZone>("kg");

  const isMountedRef = useRef(true);
  const activeZoneRef = useRef<GraphZone>("kg");
  const selectedDatasourceCodeRef = useRef<string | undefined>(undefined);
  const zoneCacheRef = useRef<ZoneCache>(createEmptyZoneCache());
  const framesRef = useRef<QueryFrame[]>([]);
  const pageLoadingRequestIdRef = useRef(0);
  const frameLoadingRequestIdsRef = useRef<Record<string, number>>({});
  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  const beginPageLoading = useCallback(() => {
    const requestId = pageLoadingRequestIdRef.current + 1;
    pageLoadingRequestIdRef.current = requestId;
    if (isMountedRef.current) setPageLoading(true);
    return requestId;
  }, []);

  const endPageLoading = useCallback((requestId: number) => {
    if (isMountedRef.current && pageLoadingRequestIdRef.current === requestId) {
      setPageLoading(false);
    }
  }, []);

  const beginFrameLoading = useCallback((frameId: string) => {
    const requestId = (frameLoadingRequestIdsRef.current[frameId] ?? 0) + 1;
    frameLoadingRequestIdsRef.current[frameId] = requestId;
    if (isMountedRef.current) {
      setFrameLoadingIds((prev) => ({ ...prev, [frameId]: true }));
    }
    return requestId;
  }, []);

  const endFrameLoading = useCallback((frameId: string, requestId: number) => {
    if (!isMountedRef.current || frameLoadingRequestIdsRef.current[frameId] !== requestId) return;
    setFrameLoadingIds((prev) => {
      const next = { ...prev };
      delete next[frameId];
      return next;
    });
  }, []);

  const setVisibleFrames = useCallback(
    (
      nextFrames: QueryFrame[],
      options?: {
        zone?: GraphZone;
        datasourceCode?: string;
        cache?: boolean;
      },
    ) => {
      const zone = options?.zone ?? activeZoneRef.current;
      const datasourceCode = getDatasourceCodeForZone(
        zone,
        options?.datasourceCode ?? selectedDatasourceCodeRef.current,
      );
      framesRef.current = nextFrames;
      if (options?.cache !== false) {
        zoneCacheRef.current[getZoneCacheKey(zone, datasourceCode)] = nextFrames;
      }
      setFrames(nextFrames);
    },
    [],
  );

  const updateVisibleFrames = useCallback(
    (
      updater: (prev: QueryFrame[]) => QueryFrame[],
      options?: {
        zone?: GraphZone;
        datasourceCode?: string;
        cache?: boolean;
      },
    ) => {
      const zone = options?.zone ?? activeZoneRef.current;
      const datasourceCode = getDatasourceCodeForZone(
        zone,
        options?.datasourceCode ?? selectedDatasourceCodeRef.current,
      );
      setFrames((prev) => {
        const updated = updater(prev);
        framesRef.current = updated;
        if (options?.cache !== false) {
          zoneCacheRef.current[getZoneCacheKey(zone, datasourceCode)] = updated;
        }
        return updated;
      });
    },
    [],
  );

  const handleExecuteQuery = useCallback(async (query: string, frameId?: string) => {
    const requestZone = activeZoneRef.current;
    const requestDatasourceCode = getDatasourceCodeForZone(
      requestZone,
      selectedDatasourceCodeRef.current,
    );
    if (requestZone !== "kg" && !requestDatasourceCode) {
      setCypherError(t("kgBrowser.selectDatasourceFirst"));
      return;
    }
    const requestCacheKey = getZoneCacheKey(requestZone, requestDatasourceCode);
    const requestFrames = framesRef.current;
    const pageRequestId = frameId ? null : beginPageLoading();
    const frameRequestId = frameId ? beginFrameLoading(frameId) : null;

    try {
      if (isMountedRef.current) {
        if (frameId) {
          setFrameErrors((prev) => { const next = { ...prev }; delete next[frameId]; return next; });
        } else {
          setCypherError(null);
        }
      }
      // Use "auto" so the server decides: graph data is returned
      // for Node/Relationship/Path results, and null for scalar-only
      // results (e.g. RETURN count(n)).
      const response = await withTimeout(
        cypherApi.execute({
          cypher: query,
          response_format: "auto",
          limit: 100,
          ...(requestDatasourceCode
            ? { datasource_id: requestDatasourceCode }
            : {}),
        }),
        REQUEST_TIMEOUT_MS,
      );

      if (!isMountedRef.current) return;

      // Extract graph data from the response graph field
      const allNodes = (response.graph?.nodes ?? []).map(apiNodeToGraphNode);
      const allEdges = (response.graph?.edges ?? []).map(apiEdgeToGraphEdge);

      // Auto-select default view: graph if we have graph data, otherwise table.
      const hasGraphData = allNodes.length > 0 || allEdges.length > 0;
      const activeTab: ViewTab = hasGraphData ? "graph" : "table";

      // Split the graph into connected components so each subgraph
      // (or isolated-node-type group) gets its own result card.
      // When graph is null (scalar results), splitGraphIntoComponents
      // returns a single empty group — the frame will use table/text view.
      const components = splitGraphIntoComponents(allNodes, allEdges);
      const baseTime = Date.now();
      const total = components.length;

      // Build common metadata shared across all frames
      const queryRows = response.rows ?? [];
      const queryColumns = response.columns?.length
        ? response.columns
        : Object.keys(queryRows[0] ?? {});
      const hasQueryRows = queryRows.length > 0 || queryColumns.length > 0;
      const commonMeta = {
        query,
        totalRecords: response.count ?? queryRows.length,
        queryType: response.summary?.result_type,
        counters: {
          node_count: response.summary?.node_count ?? allNodes.length,
          edge_count: response.summary?.edge_count ?? allEdges.length,
        },
        labelsHit: response.summary?.labels_hit,
        elapsedMs: response.summary?.elapsed_ms,
      };

      const newFrames: QueryFrame[] = components.map((comp, index) => {
        const { rows, columns } = hasQueryRows
          ? { rows: queryRows, columns: queryColumns }
          : buildRowsFromNodes(comp.nodes);
        return {
          id: `frame-${baseTime}-${index}-${Math.random().toString(16).slice(2)}`,
          query,
          subTitle: total > 1 ? `#${index + 1} / ${total}` : undefined,
          timestamp: baseTime,
          activeTab,
          result: comp,
          rows,
          columns,
          meta: commonMeta,
        };
      });

      if (frameId) {
        const sourceFrames = zoneCacheRef.current[requestCacheKey] ?? requestFrames;
        const idx = sourceFrames.findIndex((f) => f.id === frameId);
        if (idx === -1) return;

        const updated = [...sourceFrames.slice(0, idx), ...newFrames, ...sourceFrames.slice(idx + 1)];
        const isActiveContext =
          activeZoneRef.current === requestZone &&
          getDatasourceCodeForZone(
            requestZone,
            selectedDatasourceCodeRef.current,
          ) === requestDatasourceCode;
        if (isActiveContext) {
          setVisibleFrames(updated, {
            zone: requestZone,
            datasourceCode: requestDatasourceCode,
          });
        } else {
          zoneCacheRef.current[requestCacheKey] = updated;
        }
      } else {
        setFrameLoadingIds({});
        const isActiveContext =
          activeZoneRef.current === requestZone &&
          getDatasourceCodeForZone(
            requestZone,
            selectedDatasourceCodeRef.current,
          ) === requestDatasourceCode;
        if (isActiveContext) {
          setVisibleFrames(newFrames, {
            zone: requestZone,
            datasourceCode: requestDatasourceCode,
          });
        } else {
          zoneCacheRef.current[requestCacheKey] = newFrames;
        }
      }
    } catch (error) {
      if (!isMountedRef.current) return;
      const msg = error instanceof Error ? error.message : t("kgBrowser.genericError");
      const isActiveContext =
        activeZoneRef.current === requestZone &&
        getDatasourceCodeForZone(
          requestZone,
          selectedDatasourceCodeRef.current,
        ) === requestDatasourceCode;
      if (!isActiveContext) {
        return;
      }
      if (frameId) {
        setFrameErrors((prev) => ({ ...prev, [frameId]: msg }));
      } else if (requestFrames.length > 0) {
        message.error(msg);
      } else {
        setCypherError(msg);
      }
      console.error("Cypher query failed:", error);
    } finally {
      if (frameId && frameRequestId !== null) {
        endFrameLoading(frameId, frameRequestId);
      }
      if (pageRequestId !== null) {
        endPageLoading(pageRequestId);
      }
    }
  }, [beginFrameLoading, beginPageLoading, endFrameLoading, endPageLoading, setVisibleFrames, t]);

  /** Load graph data for a specific zone (KG / TG / MG).
   *  Uses explorerApi.getGlobalGraph with zone_mode to fetch only the
   *  selected layer's nodes and edges, then splits into components for
   *  multi-card display — same rendering pipeline as handleExecuteQuery. */
  const handleZoneChange = useCallback(async (zone: GraphZone, options?: { forceReload?: boolean }) => {
    const zoneConfig = ZONE_TABS.find((z) => z.key === zone);
    if (!zoneConfig) return;
    const forceReload = options?.forceReload ?? false;
    const previousZone = activeZoneRef.current;
    const datasourceCode = getDatasourceCodeForZone(
      zone,
      selectedDatasourceCodeRef.current,
    );
    const requestCacheKey = getZoneCacheKey(zone, datasourceCode);

    // Save the currently visible cards before moving to another zone.
    if (previousZone !== zone) {
      const previousDatasourceCode = getDatasourceCodeForZone(
        previousZone,
        selectedDatasourceCodeRef.current,
      );
      zoneCacheRef.current[
        getZoneCacheKey(previousZone, previousDatasourceCode)
      ] = framesRef.current;
    }

    activeZoneRef.current = zone;
    setActiveZone(zone);

    // Restore from cache if available
    const cached = zoneCacheRef.current[requestCacheKey] ?? null;
    if (!forceReload && cached !== null) {
      setVisibleFrames(cached, { zone, datasourceCode });
      return;
    }
    if (previousZone !== zone) {
      setVisibleFrames([], { zone, datasourceCode, cache: false });
    }

    // TG/MG requests must always be scoped to a selected datasource.
    if (zone !== "kg" && !datasourceCode) {
      setVisibleFrames([], { zone, datasourceCode, cache: false });
      return;
    }

    const requestId = beginPageLoading();

    try {
      if (isMountedRef.current && activeZoneRef.current === zone) {
        setCypherError(null);
      }

      const graphData = await withTimeout(
        explorerApi.getGlobalGraph({
          zone_mode: zoneConfig.zoneMode,
          max_nodes: 200,
          max_edges: 500,
          skeleton: false,
          ...(datasourceCode ? { datasource_id: datasourceCode } : {}),
        }),
        REQUEST_TIMEOUT_MS,
      );

      if (!isMountedRef.current) return;

      const allNodes = graphData.nodes.map(apiNodeToGraphNode);
      const allEdges = graphData.edges.map(apiEdgeToGraphEdge);

      const isActiveContext =
        activeZoneRef.current === zone &&
        getDatasourceCodeForZone(
          zone,
          selectedDatasourceCodeRef.current,
        ) === datasourceCode;

      if (allNodes.length === 0) {
        if (isActiveContext) {
          setVisibleFrames([], { zone, datasourceCode });
        } else {
          zoneCacheRef.current[requestCacheKey] = [];
        }
        return;
      }

      const components = splitGraphIntoComponents(allNodes, allEdges);
      const baseTime = Date.now();
      const total = components.length;
      const queryLabel = `${zoneConfig.zoneMode} graph`;

      const newFrames: QueryFrame[] = components.map((comp, index) => {
        const { rows, columns } = buildRowsFromNodes(comp.nodes);
        return {
          id: `frame-${baseTime}-${index}-${Math.random().toString(16).slice(2)}`,
          query: queryLabel,
          subTitle: total > 1 ? `#${index + 1} / ${total}` : undefined,
          timestamp: baseTime,
          activeTab: "graph" as ViewTab,
          result: comp,
          rows,
          columns,
          meta: {
            query: queryLabel,
            totalRecords: allNodes.length,
            queryType: "zone_graph",
            counters: {
              node_count: allNodes.length,
              edge_count: allEdges.length,
            },
          },
        };
      });

      if (isActiveContext) {
        setVisibleFrames(newFrames, { zone, datasourceCode });
      } else {
        zoneCacheRef.current[requestCacheKey] = newFrames;
      }
    } catch (error) {
      if (!isMountedRef.current) return;
      const msg = error instanceof Error ? error.message : t("kgBrowser.genericError");
      const isActiveContext =
        activeZoneRef.current === zone &&
        getDatasourceCodeForZone(
          zone,
          selectedDatasourceCodeRef.current,
        ) === datasourceCode;
      if (!isActiveContext) {
        return;
      }
      if (framesRef.current.length === 0) {
        setCypherError(msg);
      } else {
        message.error(msg);
      }
      console.error("Zone graph query failed:", error);
    } finally {
      endPageLoading(requestId);
    }
  }, [beginPageLoading, endPageLoading, setVisibleFrames, t]);

  /** Switch zone tab without triggering a data load.
   *  Used before executing a Cypher query so the tab updates instantly
   *  and the query result is cached under the correct zone. */
  const switchZoneTab = useCallback((zone: GraphZone) => {
    const previousZone = activeZoneRef.current;
    if (previousZone !== zone) {
      const previousDatasourceCode = getDatasourceCodeForZone(
        previousZone,
        selectedDatasourceCodeRef.current,
      );
      zoneCacheRef.current[
        getZoneCacheKey(previousZone, previousDatasourceCode)
      ] = framesRef.current;
    }
    activeZoneRef.current = zone;
    setActiveZone(zone);
    const datasourceCode = getDatasourceCodeForZone(
      zone,
      selectedDatasourceCodeRef.current,
    );
    const cached = zoneCacheRef.current[getZoneCacheKey(zone, datasourceCode)] ?? null;
    setVisibleFrames(cached ?? [], {
      zone,
      datasourceCode,
      cache: cached !== null,
    });
  }, [setVisibleFrames]);

  /** Retry loading the current zone graph (used by empty / error states). */
  const handleRetry = useCallback(() => {
    handleZoneChange(activeZoneRef.current, { forceReload: true });
  }, [handleZoneChange]);

  // Auto-load KG zone graph on page mount
  useEffect(() => {
    handleZoneChange("kg");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Build a Cypher query for a node label tag click. */
  const handleLabelClick = useCallback(
    (label: string, zone?: string) => {
      const targetZone = zone ? ZONE_MODE_TO_KEY[zone] : undefined;
      if (targetZone && targetZone !== activeZoneRef.current) {
        switchZoneTab(targetZone);
      }
      const query =
        label === "*"
          ? "MATCH (n) RETURN n LIMIT 50"
          : `MATCH (n:\`${label}\`) RETURN n LIMIT 50`;
      handleExecuteQuery(query);
    },
    [handleExecuteQuery, switchZoneTab],
  );

  /** Build a Cypher query for a relationship type tag click. */
  const handleRelTypeClick = useCallback(
    (relType: string, zone?: string) => {
      const targetZone = zone ? ZONE_MODE_TO_KEY[zone] : undefined;
      if (targetZone && targetZone !== activeZoneRef.current) {
        switchZoneTab(targetZone);
      }
      const query =
        relType === "*"
          ? "MATCH p=()-[r]->() RETURN p LIMIT 50"
          : `MATCH p=()-[r:${relType}]->() RETURN p LIMIT 50`;
      handleExecuteQuery(query);
    },
    [handleExecuteQuery, switchZoneTab],
  );

  const handleTabChange = useCallback((frameId: string, tab: ViewTab) => {
    updateVisibleFrames((prev) =>
      prev.map((f) => (f.id === frameId ? { ...f, activeTab: tab } : f)),
    );
  }, [updateVisibleFrames]);

  const handleCloseFrame = useCallback((frameId: string) => {
    updateVisibleFrames((prev) => prev.filter((f) => f.id !== frameId));
    setFrameLoadingIds((prev) => {
      const next = { ...prev };
      delete next[frameId];
      return next;
    });
    setFrameErrors((prev) => {
      const next = { ...prev };
      delete next[frameId];
      return next;
    });
  }, [updateVisibleFrames]);

  /** Update a node's properties inside a frame after inline editing. */
  const handleNodeUpdate = useCallback(
    (frameId: string, nodeId: string, properties: Record<string, unknown>) => {
      updateVisibleFrames((prev) =>
        prev.map((f) =>
          f.id === frameId
            ? {
                ...f,
                result: {
                  ...f.result,
                  nodes: f.result.nodes.map((n) =>
                    n.id === nodeId ? { ...n, properties } : n,
                  ),
                },
              }
            : f,
        ),
      );
    },
    [updateVisibleFrames],
  );

  /** Update an edge's properties inside a frame after inline editing. */
  const handleEdgeUpdate = useCallback(
    (frameId: string, edgeId: string, properties: Record<string, unknown>) => {
      updateVisibleFrames((prev) =>
        prev.map((f) =>
          f.id === frameId
            ? {
                ...f,
                result: {
                  ...f.result,
                  edges: f.result.edges.map((e) =>
                    e.id === edgeId ? { ...e, properties } : e,
                  ),
                },
              }
            : f,
        ),
      );
    },
    [updateVisibleFrames],
  );

  /** Switch datasource context and reload the current TG/MG graph. */
  const handleDatabaseChange = useCallback((datasourceCode?: string) => {
    const normalizedCode = datasourceCode?.trim() || undefined;
    const previousCode = selectedDatasourceCodeRef.current;
    const currentZone = activeZoneRef.current;

    if (previousCode === normalizedCode) {
      setSelectedDatasourceCode(normalizedCode);
      return;
    }

    if (currentZone !== "kg") {
      zoneCacheRef.current[getZoneCacheKey(currentZone, previousCode)] =
        framesRef.current;
    }
    selectedDatasourceCodeRef.current = normalizedCode;
    setSelectedDatasourceCode(normalizedCode);

    if (currentZone !== "kg") {
      setVisibleFrames([], {
        zone: currentZone,
        datasourceCode: normalizedCode,
        cache: false,
      });
      void handleZoneChange(currentZone, { forceReload: true });
    }
  }, [handleZoneChange, setVisibleFrames]);

  /** Handle subgraph search results from the sidebar search tab.
   *  Converts API nodes/edges to graph format, splits into connected
   *  components, and renders each as a separate result frame. */
  const handleSubgraphSearch = useCallback(
    (result: SubgraphSearchResult) => {
      if (!isMountedRef.current) return;

      const allNodes = result.nodes.map(apiNodeToGraphNode);
      const allEdges = result.edges.map(apiEdgeToGraphEdge);

      if (allNodes.length === 0) {
        setFrameLoadingIds({});
        setVisibleFrames([]);
        return;
      }
      const components = splitGraphIntoComponents(allNodes, allEdges);
      const baseTime = Date.now();
      const total = components.length;
      const queryLabel = `search: "${result.query}"`;

      const newFrames: QueryFrame[] = components.map((comp, index) => {
        // Build per-component rows/columns so each card's table view
        // only shows the nodes belonging to that component.
        const { rows, columns } = buildRowsFromNodes(comp.nodes);
        return {
          id: `frame-${baseTime}-${index}-${Math.random().toString(16).slice(2)}`,
          query: queryLabel,
          subTitle: total > 1 ? `#${index + 1} / ${total}` : undefined,
          timestamp: baseTime,
          activeTab: "graph" as ViewTab,
          result: comp,
          rows,
          columns,
          meta: {
            query: queryLabel,
            totalRecords: allNodes.length,
            queryType: "subgraph_search",
            counters: {
              node_count: allNodes.length,
              edge_count: allEdges.length,
            },
          },
        };
      });

      setFrameLoadingIds({});
      setVisibleFrames(newFrames);
    },
    [setVisibleFrames],
  );

  return (
    <div className={styles.page}>
      {/* Left sidebar */}
      <DatabaseSidebar
        showDatabaseSelector={activeZone !== "kg"}
        onLabelClick={handleLabelClick}
        onRelTypeClick={handleRelTypeClick}
        onDatasourceCodeChange={handleDatabaseChange}
        onSubgraphSearch={handleSubgraphSearch}
      />

      {/* Main content area */}
      <div className={styles.mainContent}>
        {/* Zone tabs bar — KG / TG / MG layer switcher */}
        <div className={styles.zoneTabsBar}>
          {ZONE_TABS.map((zt) => {
            const editableText = zt.editableLabels.length > 0
              ? `${t("kgBrowser.editableRange")}: ${zt.editableLabels.join(", ")}`
              : t("kgBrowser.allReadonly");
            const readonlyText = zt.readonlyLabels.length > 0
              ? `${t("kgBrowser.readonlyRange")}: ${zt.readonlyLabels.join(", ")}`
              : "";
            return (
              <Tooltip
                key={zt.key}
                title={
                  <div>
                    <div>{t(`kgBrowser.zone_${zt.key}_desc`)}</div>
                    <div>{editableText}</div>
                    {readonlyText && <div>{readonlyText}</div>}
                  </div>
                }
              >
                <div
                  className={`${styles.zoneTab} ${activeZone === zt.key ? styles.zoneTabActive : ""}`}
                  onClick={() => handleZoneChange(zt.key)}
                >
                  <span className={styles.zoneTabKey}>{zt.key.toUpperCase()}</span>
                  <span className={styles.zoneTabName}>{t(`kgBrowser.zone_${zt.key}`)}</span>
                </div>
              </Tooltip>
            );
          })}
        </div>

        {/* Scrollable frames container */}
        <div className={styles.framesContainer}>
          {pageLoading && frames.length > 0 && (
            <div className={styles.framesLoadingOverlay}>
              <Spin size="large" />
              <span className={styles.framesLoadingText}>{t("kgBrowser.queryRunning")}</span>
            </div>
          )}
          {pageLoading && frames.length === 0 ? (
            <div className={styles.framesLoading}>{t("kgBrowser.queryRunning")}</div>
          ) : frames.length === 0 ? (
            <div className={styles.framesInitial}>
              <div className={styles.framesInitialQueryBar}>
                <span className={styles.frameQueryPrefix}>
                  {selectedDatasourceCode ? `${selectedDatasourceCode}$` : "$"}
                </span>
                <Input
                  value={initialQuery}
                  onChange={(e) => setInitialQuery(e.target.value)}
                  onPressEnter={() => handleExecuteQuery(initialQuery)}
                  className={styles.frameQueryInput}
                  variant="borderless"
                  size="small"
                  placeholder={t("kgBrowser.cypherPlaceholder")}
                />
                <Button
                  type="primary"
                  icon={<CaretRightOutlined />}
                  size="small"
                  onClick={() => handleExecuteQuery(initialQuery)}
                  loading={pageLoading}
                  disabled={pageLoading}
                  className={styles.frameExecuteBtn}
                />
              </div>
              {cypherError && (
                <div className={styles.frameErrorPanel}>
                  <WarningOutlined className={styles.frameErrorIcon} />
                  <span className={styles.frameErrorText}>{cypherError}</span>
                  <CloseOutlined className={styles.frameErrorClose} onClick={() => setCypherError(null)} />
                </div>
              )}
              <Empty
                description={t("kgBrowser.noQueryResultsYet")}
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              >
                <Button
                  type="primary"
                  icon={<ReloadOutlined />}
                  onClick={handleRetry}
                  loading={pageLoading}
                >
                  {t("kgBrowser.retry")}
                </Button>
              </Empty>
            </div>
          ) : (
            frames.map((frame) => (
              <ResultFrame
                key={frame.id}
                frame={frame}
                onTabChange={handleTabChange}
                onClose={handleCloseFrame}
                onNodeUpdate={handleNodeUpdate}
                onEdgeUpdate={handleEdgeUpdate}
                onExecuteQuery={handleExecuteQuery}
                errorMessage={frameErrors[frame.id]}
                onDismissError={(fid) => setFrameErrors((prev) => { const next = { ...prev }; delete next[fid]; return next; })}
                loading={!!frameLoadingIds[frame.id]}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
