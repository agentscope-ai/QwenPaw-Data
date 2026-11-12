import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Empty, Button, Input, Tooltip, Spin, message } from '@/design';
import { ReloadOutlined, CaretRightOutlined, WarningOutlined, CloseOutlined } from '@/design';
import DatabaseSidebar from './components/DatabaseSidebar';
import type { SubgraphSearchResult } from './components/DatabaseSidebar';
import ResultFrame from './components/ResultFrame';
import MgSyncBar from '@/components/MgSyncBar';
import UserGraphPanel from './components/UserGraphPanel';
import { cypherApi, explorerApi, apiNodeToGraphNode, apiEdgeToGraphEdge } from '@/services/cmgraph';
import { useAuthStore } from '@/store/authStore';
import OrgBreadcrumb from '@/pages/GraphManagement/OrgBreadcrumb';
import type { OrgCrumb } from '@/pages/GraphManagement/OrgBreadcrumb';
import { GRAPH_MANAGEMENT_PATH, workspaceDetailPath } from '@/pages/GraphManagement/orgNav';
import type { QueryFrame, ViewTab, GraphZone, ZoneTabConfig } from './types';
import type { UserGraphMemoryQuery } from './components/UserGraphPanel/useUserGraph';
import { splitGraphIntoComponents, buildRowsFromNodes } from './utils/graphUtils';
import styles from './index.module.less';
import { createUrlEnumParser, readUrlSearchValues } from '@/utils/urlSearch';

/** Zone tab configuration — editable / readonly ranges per layer.
 *  KG: all node & edge properties editable.
 *  MG: everything read-only. */
const ZONE_TABS: ZoneTabConfig[] = [
  {
    key: 'kg',
    zoneMode: 'knowledge',
    editableLabels: ['Entity', 'Event'],
    readonlyLabels: [],
    allEdgesEditable: true,
  },
  {
    key: 'mg',
    zoneMode: 'metadata',
    editableLabels: [],
    readonlyLabels: ['Domain', 'Metric', 'Dimension', 'Dataset'],
    allEdgesEditable: false,
  },
];

/** Default Cypher query executed on initial load. */
const DEFAULT_QUERY = 'MATCH (n) RETURN n LIMIT 50';
const REQUEST_TIMEOUT_MS = 10000;
const MEMORY_TYPES = ['principle', 'preference', 'anchored_rule', 'trigger'] as const;
const MEMORY_STATUSES = ['active', 'needs_review', 'revoked'] as const;

/** Map API zone mode strings to GraphZone tab keys. */
const ZONE_MODE_TO_KEY: Record<string, GraphZone> = {
  knowledge: 'kg',
  metadata: 'mg',
};

type ZoneCache = Record<string, QueryFrame[] | null>;
type CMGraphView = 'ug' | 'graph';

function getZoneCacheKey(zone: GraphZone, datasourceCode?: string): string {
  return zone === 'kg' ? zone : `${zone}:${datasourceCode?.trim() || '__unselected__'}`;
}

function getDatasourceCodeForZone(zone: GraphZone, datasourceCode?: string): string | undefined {
  if (zone === 'kg') return undefined;
  return datasourceCode?.trim() || undefined;
}

const createEmptyZoneCache = (): ZoneCache => ({ kg: null });

interface PersistedGraphState {
  activeZone: GraphZone;
  selectedDatasourceCode?: string;
  initialQuery: string;
  zones: ZoneCache;
}

function isGraphZone(value: unknown): value is GraphZone {
  return value === 'kg' || value === 'mg';
}

function readPersistedGraphState(key: string): PersistedGraphState | null {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(key) || 'null') as
      (Partial<PersistedGraphState> & { selectedDatabase?: unknown }) | null;
    if (
      !parsed ||
      !isGraphZone(parsed.activeZone) ||
      !parsed.zones ||
      typeof parsed.zones !== 'object'
    )
      return null;
    const zones: ZoneCache = {};
    for (const [zoneKey, frames] of Object.entries(parsed.zones)) {
      if (frames === null || Array.isArray(frames)) {
        zones[zoneKey] = frames;
      }
    }
    zones.kg ??= null;
    return {
      activeZone: parsed.activeZone,
      selectedDatasourceCode:
        typeof parsed.selectedDatasourceCode === 'string'
          ? parsed.selectedDatasourceCode
          : typeof parsed.selectedDatabase === 'string'
            ? parsed.selectedDatabase
            : undefined,
      initialQuery: typeof parsed.initialQuery === 'string' ? parsed.initialQuery : DEFAULT_QUERY,
      zones,
    };
  } catch {
    window.sessionStorage.removeItem(key);
    return null;
  }
}

function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  timeoutMessage: string,
): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      window.setTimeout(() => reject(new Error(timeoutMessage)), timeoutMs);
    }),
  ]);
}

export default function CMGraphPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [initialMemoryQuery] = useState(() =>
    readUrlSearchValues<Partial<UserGraphMemoryQuery>>(searchParams, {
      type: {
        param: 'memory_type',
        aliases: ['memoryType'],
        parse: createUrlEnumParser(MEMORY_TYPES),
      },
      status: {
        param: 'memory_status',
        aliases: ['memoryStatus'],
        parse: createUrlEnumParser(MEMORY_STATUSES),
      },
      q: { param: 'memory_q', aliases: ['memoryQ'] },
    }),
  );
  const tenantId = useAuthStore((state) => state.tenantId);
  const workspaceId = useAuthStore((state) => state.activeWorkspaceId);
  const userId = useAuthStore((state) => state.userId);
  const authEnabled = useAuthStore((state) => state.authEnabled);
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const isAdmin = useAuthStore((state) => state.isAdmin);
  const activeWorkspaceName = useAuthStore((state) => state.activeWorkspaceName);
  const canEditOwnKG = !authEnabled || capabilities.includes('tg_kg:self');
  // Member userspace browsing is driven by URL params set from the
  // graph-management member list; only admins may target another owner.
  const browsingMemberId = (isAdmin && searchParams.get('memberId')) || null;
  const browsingMemberName = browsingMemberId
    ? searchParams.get('memberName') || browsingMemberId
    : '';
  const zoneParam = searchParams.get('zone');
  const initialZone: GraphZone | null = isGraphZone(zoneParam) ? zoneParam : null;
  // The global MG entry (an admin with no member selected) is an MG-only view:
  // admins have no personal KG userspace, so the KG layers do not apply.
  const isGlobalMgView = authEnabled && isAdmin && !browsingMemberId;
  const targetOwnerId = isGlobalMgView ? '__global__' : (browsingMemberId ?? undefined);
  const isDrilldownReadOnly = authEnabled && isAdmin && Boolean(browsingMemberId);
  // Cypher stays blocked only in the member drill-down: an admin's scoped query
  // carries no owner predicate, so a free query there would silently widen the
  // result past the single member the view claims to be showing. The global MG
  // view sends scope=global_mg instead, which pins the owner server-side.
  const isQueryDisabled = isDrilldownReadOnly;
  const graphStorageKey = `qwenpaw-data:workspace:${tenantId}:${workspaceId}:${userId}:cm-graph:${targetOwnerId ?? 'self'}:v2`;

  const [frames, setFrames] = useState<QueryFrame[]>([]);
  const [pageLoading, setPageLoading] = useState(false);
  const [frameLoadingIds, setFrameLoadingIds] = useState<Record<string, boolean>>({});
  const [cypherError, setCypherError] = useState<string | null>(null);
  const [frameErrors, setFrameErrors] = useState<Record<string, string>>({});
  const [selectedDatasourceCode, setSelectedDatasourceCode] = useState<string>();
  const [initialQuery, setInitialQuery] = useState(DEFAULT_QUERY);
  const [activeZone, setActiveZone] = useState<GraphZone>('kg');
  const [activeView, setActiveView] = useState<CMGraphView>(
    isGlobalMgView || initialZone ? 'graph' : 'ug',
  );

  const isMountedRef = useRef(true);
  const activeZoneRef = useRef<GraphZone>('kg');
  const selectedDatasourceCodeRef = useRef<string>();
  const targetOwnerIdRef = useRef<string | undefined>(targetOwnerId);
  targetOwnerIdRef.current = targetOwnerId;
  const graphStorageKeyRef = useRef(graphStorageKey);
  graphStorageKeyRef.current = graphStorageKey;
  const zoneCacheRef = useRef<ZoneCache>(createEmptyZoneCache());
  const framesRef = useRef<QueryFrame[]>([]);
  const hydratedStorageKeyRef = useRef('');
  const skipNextPersistRef = useRef(false);
  const pageLoadingRequestIdRef = useRef(0);
  const frameLoadingRequestIdsRef = useRef<Record<string, number>>({});
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // The global MG entry renders no UG tab, so pin the view to the graph even
  // when the auth store hydrates after the first render.
  useEffect(() => {
    if (isGlobalMgView) setActiveView('graph');
  }, [isGlobalMgView]);

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

  const handleExecuteQuery = useCallback(
    async (query: string, frameId?: string) => {
      if (isQueryDisabled) {
        message.warning(t('kgBrowser.drilldownQueryDisabled'));
        return;
      }
      const requestZone = activeZoneRef.current;
      const requestDatasourceCode = getDatasourceCodeForZone(
        requestZone,
        selectedDatasourceCodeRef.current,
      );
      if (requestZone !== 'kg' && !requestDatasourceCode) {
        setCypherError(t('kgBrowser.selectDatasourceFirst'));
        return;
      }
      const requestCacheKey = getZoneCacheKey(requestZone, requestDatasourceCode);
      const requestFrames = framesRef.current;
      const pageRequestId = frameId ? null : beginPageLoading();
      const frameRequestId = frameId ? beginFrameLoading(frameId) : null;

      try {
        if (isMountedRef.current) {
          if (frameId) {
            setFrameErrors((prev) => {
              const next = { ...prev };
              delete next[frameId];
              return next;
            });
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
            response_format: 'auto',
            limit: 200,
            ...(isGlobalMgView ? { scope: 'global_mg' as const } : {}),
            ...(requestDatasourceCode ? { datasource_id: requestDatasourceCode } : {}),
          }),
          REQUEST_TIMEOUT_MS,
          t('kgBrowser.requestTimeout'),
        );

        if (!isMountedRef.current) return;

        // Extract graph data from the response graph field
        const allNodes = (response.graph?.nodes ?? []).map(apiNodeToGraphNode);
        const allEdges = (response.graph?.edges ?? []).map(apiEdgeToGraphEdge);

        // Auto-select default view: graph if we have graph data, otherwise table.
        const hasGraphData = allNodes.length > 0 || allEdges.length > 0;
        const activeTab: ViewTab = hasGraphData ? 'graph' : 'table';

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

          const updated = [
            ...sourceFrames.slice(0, idx),
            ...newFrames,
            ...sourceFrames.slice(idx + 1),
          ];
          const isActiveContext =
            activeZoneRef.current === requestZone &&
            getDatasourceCodeForZone(requestZone, selectedDatasourceCodeRef.current) ===
              requestDatasourceCode;
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
            getDatasourceCodeForZone(requestZone, selectedDatasourceCodeRef.current) ===
              requestDatasourceCode;
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
        const msg = error instanceof Error ? error.message : t('kgBrowser.genericError');
        const isActiveContext =
          activeZoneRef.current === requestZone &&
          getDatasourceCodeForZone(requestZone, selectedDatasourceCodeRef.current) ===
            requestDatasourceCode;
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
        console.error('Cypher query failed:', error);
      } finally {
        if (frameId && frameRequestId !== null) {
          endFrameLoading(frameId, frameRequestId);
        }
        if (pageRequestId !== null) {
          endPageLoading(pageRequestId);
        }
      }
    },
    [
      beginFrameLoading,
      beginPageLoading,
      endFrameLoading,
      endPageLoading,
      isGlobalMgView,
      isQueryDisabled,
      setVisibleFrames,
      t,
    ],
  );

  /** Load graph data for a specific zone (KG / TG / MG).
   *  Uses explorerApi.getGlobalGraph with zone_mode to fetch only the
   *  selected layer's nodes and edges, then splits into components for
   *  multi-card display — same rendering pipeline as handleExecuteQuery. */
  const handleZoneChange = useCallback(
    async (zone: GraphZone, options?: { forceReload?: boolean }) => {
      const zoneConfig = ZONE_TABS.find((z) => z.key === zone);
      if (!zoneConfig) return;
      const forceReload = options?.forceReload ?? false;
      const previousZone = activeZoneRef.current;
      const datasourceCode = getDatasourceCodeForZone(zone, selectedDatasourceCodeRef.current);
      const requestOwnerId = targetOwnerIdRef.current;
      const requestGraphStorageKey = graphStorageKeyRef.current;
      const requestCacheKey = getZoneCacheKey(zone, datasourceCode);

      // Save the currently visible cards before moving to another zone.
      if (previousZone !== zone) {
        const previousDatasourceCode = getDatasourceCodeForZone(
          previousZone,
          selectedDatasourceCodeRef.current,
        );
        zoneCacheRef.current[getZoneCacheKey(previousZone, previousDatasourceCode)] =
          framesRef.current;
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

      // MG requests must always be scoped to a selected datasource.
      if (zone !== 'kg' && !datasourceCode) {
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
            // 与全局图画布同口径：旧上限 200/500 会把整库编织结果
            // （如 ~470 点 / ~550 边）拦腰截断，边还按表序偏向丢。
            max_nodes: 800,
            max_edges: 3000,
            skeleton: false,
            ...(datasourceCode ? { datasource_id: datasourceCode } : {}),
            ...(requestOwnerId ? { owner_id: requestOwnerId } : {}),
          }),
          REQUEST_TIMEOUT_MS,
          t('kgBrowser.requestTimeout'),
        );

        if (!isMountedRef.current) return;

        const allNodes = graphData.nodes.map(apiNodeToGraphNode);
        const allEdges = graphData.edges.map(apiEdgeToGraphEdge);

        const isSameGraphContext =
          graphStorageKeyRef.current === requestGraphStorageKey &&
          targetOwnerIdRef.current === requestOwnerId;
        const isActiveContext =
          isSameGraphContext &&
          activeZoneRef.current === zone &&
          getDatasourceCodeForZone(zone, selectedDatasourceCodeRef.current) === datasourceCode;

        if (allNodes.length === 0) {
          if (isActiveContext) {
            setVisibleFrames([], { zone, datasourceCode });
          } else if (isSameGraphContext) {
            zoneCacheRef.current[requestCacheKey] = [];
          }
          return;
        }

        const components = splitGraphIntoComponents(allNodes, allEdges);
        const baseTime = Date.now();
        const total = components.length;
        const queryLabel = t('kgBrowser.zoneGraphQuery', {
          zone: t(`kgBrowser.zone_${zoneConfig.key}`),
        });

        const newFrames: QueryFrame[] = components.map((comp, index) => {
          const { rows, columns } = buildRowsFromNodes(comp.nodes);
          return {
            id: `frame-${baseTime}-${index}-${Math.random().toString(16).slice(2)}`,
            query: queryLabel,
            subTitle: total > 1 ? `#${index + 1} / ${total}` : undefined,
            timestamp: baseTime,
            activeTab: 'graph' as ViewTab,
            result: comp,
            rows,
            columns,
            meta: {
              query: queryLabel,
              totalRecords: allNodes.length,
              queryType: 'zone_graph',
              counters: {
                node_count: allNodes.length,
                edge_count: allEdges.length,
              },
            },
          };
        });

        if (isActiveContext) {
          setVisibleFrames(newFrames, { zone, datasourceCode });
        } else if (isSameGraphContext) {
          zoneCacheRef.current[requestCacheKey] = newFrames;
        }
      } catch (error) {
        if (!isMountedRef.current) return;
        const msg = error instanceof Error ? error.message : t('kgBrowser.genericError');
        const isActiveContext =
          graphStorageKeyRef.current === requestGraphStorageKey &&
          targetOwnerIdRef.current === requestOwnerId &&
          activeZoneRef.current === zone &&
          getDatasourceCodeForZone(zone, selectedDatasourceCodeRef.current) === datasourceCode;
        if (!isActiveContext) {
          return;
        }
        if (framesRef.current.length === 0) {
          setCypherError(msg);
        } else {
          message.error(msg);
        }
        console.error('Zone graph query failed:', error);
      } finally {
        endPageLoading(requestId);
      }
    },
    [beginPageLoading, endPageLoading, setVisibleFrames, t],
  );

  /** Switch zone tab without triggering a data load.
   *  Used before executing a Cypher query so the tab updates instantly
   *  and the query result is cached under the correct zone. */
  const switchZoneTab = useCallback(
    (zone: GraphZone) => {
      const previousZone = activeZoneRef.current;
      if (previousZone !== zone) {
        const previousDatasourceCode = getDatasourceCodeForZone(
          previousZone,
          selectedDatasourceCodeRef.current,
        );
        zoneCacheRef.current[getZoneCacheKey(previousZone, previousDatasourceCode)] =
          framesRef.current;
      }
      activeZoneRef.current = zone;
      setActiveZone(zone);
      const datasourceCode = getDatasourceCodeForZone(zone, selectedDatasourceCodeRef.current);
      const cached = zoneCacheRef.current[getZoneCacheKey(zone, datasourceCode)] ?? null;
      setVisibleFrames(cached ?? [], {
        zone,
        datasourceCode,
        cache: cached !== null,
      });
    },
    [setVisibleFrames],
  );

  /** Retry loading the current zone graph (used by empty / error states). */
  const handleRetry = useCallback(() => {
    handleZoneChange(activeZoneRef.current, { forceReload: true });
  }, [handleZoneChange]);

  const scopeHint = useMemo(() => {
    if (!authEnabled) return null;
    if (isDrilldownReadOnly) {
      return t('kgBrowser.scope.adminBrowsing', { member: browsingMemberName });
    }
    if (activeZone === 'mg') {
      if (activeWorkspaceRole === 'tenant_admin')
        return t('kgBrowser.scope.tenantAdminMG', { workspace: activeWorkspaceName });
      if (activeWorkspaceRole === 'ws_admin') return t('kgBrowser.scope.wsAdminMG');
      return t('kgBrowser.scope.memberMG');
    }
    if (activeWorkspaceRole === 'tenant_admin')
      return t('kgBrowser.scope.tenantAdminTGKG', { workspace: activeWorkspaceName });
    if (activeWorkspaceRole === 'ws_admin') {
      return browsingMemberId
        ? t('kgBrowser.scope.adminBrowsing', { member: browsingMemberName })
        : t('kgBrowser.scope.wsAdminAll');
    }
    return t('kgBrowser.scope.memberOwn');
  }, [
    activeWorkspaceName,
    activeWorkspaceRole,
    activeZone,
    authEnabled,
    browsingMemberId,
    browsingMemberName,
    isDrilldownReadOnly,
    t,
  ]);

  /** Organization breadcrumb: tenant / workspace / member drill-down path. */
  const orgCrumbs = useMemo<OrgCrumb[]>(() => {
    if (!authEnabled) return [];
    if (activeWorkspaceRole === 'tenant_admin' || activeWorkspaceRole === 'ws_admin') {
      const crumbs: OrgCrumb[] = [];
      if (activeWorkspaceRole === 'tenant_admin') {
        crumbs.push({
          label: t('graphManagement.breadcrumb.tenant', { tenant: tenantId }),
          path: GRAPH_MANAGEMENT_PATH,
        });
      }
      crumbs.push({
        label: activeWorkspaceName || workspaceId,
        path: workspaceDetailPath(workspaceId),
      });
      crumbs.push({
        label: browsingMemberId
          ? browsingMemberName
          : zoneParam === 'mg'
            ? t('graphManagement.mgEntryCard')
            : t('graphManagement.workspaceGraphs'),
      });
      return crumbs;
    }
    return [{ label: activeWorkspaceName || workspaceId }, { label: t('kgBrowser.myGraph') }];
  }, [
    activeWorkspaceName,
    activeWorkspaceRole,
    authEnabled,
    browsingMemberId,
    browsingMemberName,
    tenantId,
    workspaceId,
    zoneParam,
    t,
  ]);

  /** Query-bar scope prefix: shows the datasource code for scoped zones
   *  (MG); KG queries carry no datasource filter, so KG shows plain "$". */
  const queryPrefix = useMemo(() => {
    const code = getDatasourceCodeForZone(activeZone, selectedDatasourceCode);
    return code ? `${code}$` : '$';
  }, [activeZone, selectedDatasourceCode]);

  /** Layer tabs offered by this view. The global MG entry is MG-only: admins
   *  have no personal KG userspace and no User Graph to manage here. */
  const visibleZoneTabs = useMemo(
    () => (isGlobalMgView ? ZONE_TABS.filter((zt) => zt.key === 'mg') : ZONE_TABS),
    [isGlobalMgView],
  );

  // Restore the last graph result for this exact user/workspace. UG remains
  // the default view and must not trigger an Explorer projection request.
  useEffect(() => {
    if (!tenantId || !workspaceId || !userId) return;
    if (hydratedStorageKeyRef.current === graphStorageKey) return;
    hydratedStorageKeyRef.current = graphStorageKey;
    skipNextPersistRef.current = true;

    const persisted = readPersistedGraphState(graphStorageKey);
    // The global MG entry is MG-only; otherwise a zone URL param (e.g. a deep
    // link) or the persisted tab selects the initial layer.
    const zone = isGlobalMgView ? 'mg' : (initialZone ?? persisted?.activeZone ?? 'kg');
    activeZoneRef.current = zone;
    setActiveZone(zone);
    if (persisted) {
      zoneCacheRef.current = persisted.zones;
      selectedDatasourceCodeRef.current = persisted.selectedDatasourceCode;
      setSelectedDatasourceCode(persisted.selectedDatasourceCode);
      setInitialQuery(persisted.initialQuery);
      const previousFrames = persisted.zones[zone];
      if (previousFrames?.length) {
        setVisibleFrames(previousFrames, { zone, cache: false });
        return;
      }
    } else {
      // Different member/user context: drop the previous owner's cache so
      // frames never leak across userspaces.
      zoneCacheRef.current = createEmptyZoneCache();
      setVisibleFrames([], { zone, cache: false });
    }
    if (activeView === 'graph') {
      void handleZoneChange(zone, { forceReload: true });
    }
  }, [
    activeView,
    graphStorageKey,
    handleZoneChange,
    initialZone,
    isGlobalMgView,
    setVisibleFrames,
    tenantId,
    userId,
    workspaceId,
  ]);

  useEffect(() => {
    if (hydratedStorageKeyRef.current !== graphStorageKey) return;
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    const state: PersistedGraphState = {
      activeZone,
      selectedDatasourceCode,
      initialQuery,
      zones: zoneCacheRef.current,
    };
    try {
      window.sessionStorage.setItem(graphStorageKey, JSON.stringify(state));
    } catch {
      // Storage quota or privacy mode: keep the in-memory cache only.
    }
  }, [activeZone, frames, graphStorageKey, initialQuery, selectedDatasourceCode]);

  /** Build a Cypher query for a node label tag click. */
  const handleLabelClick = useCallback(
    (label: string, zone?: string) => {
      const targetZone = zone ? ZONE_MODE_TO_KEY[zone] : undefined;
      if (targetZone && targetZone !== activeZoneRef.current) {
        switchZoneTab(targetZone);
      }
      const query =
        label === '*'
          ? 'MATCH (n) RETURN n LIMIT 200'
          : `MATCH (n:\`${label}\`) RETURN n LIMIT 200`;
      handleExecuteQuery(query);
    },
    [handleExecuteQuery, switchZoneTab],
  );

  /** Build a Cypher query for a relationship type tag click.
   *  端点必须具名：鉴权开启时 scope 注入器对匿名节点直接拒绝
   *  （"Anonymous nodes are not supported"），边类型筛选会整体 400。
   *  RETURN 点+边三列（而非 path），Holo AGE 的 path 序列化才能被
   *  图提取逻辑逐元素还原。 */
  const handleRelTypeClick = useCallback(
    (relType: string, zone?: string) => {
      const targetZone = zone ? ZONE_MODE_TO_KEY[zone] : undefined;
      if (targetZone && targetZone !== activeZoneRef.current) {
        switchZoneTab(targetZone);
      }
      const query =
        relType === '*'
          ? 'MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 200'
          : `MATCH (a)-[r:${relType}]->(b) RETURN a, r, b LIMIT 200`;
      handleExecuteQuery(query);
    },
    [handleExecuteQuery, switchZoneTab],
  );

  const handleTabChange = useCallback(
    (frameId: string, tab: ViewTab) => {
      updateVisibleFrames((prev) =>
        prev.map((f) => (f.id === frameId ? { ...f, activeTab: tab } : f)),
      );
    },
    [updateVisibleFrames],
  );

  const handleCloseFrame = useCallback(
    (frameId: string) => {
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
    },
    [updateVisibleFrames],
  );

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
                  nodes: f.result.nodes.map((n) => (n.id === nodeId ? { ...n, properties } : n)),
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
                  edges: f.result.edges.map((e) => (e.id === edgeId ? { ...e, properties } : e)),
                },
              }
            : f,
        ),
      );
    },
    [updateVisibleFrames],
  );

  /** Switch datasource context and reload the current MG graph. */
  const handleDatabaseChange = useCallback(
    (datasourceCode?: string) => {
      const normalizedCode = datasourceCode?.trim() || undefined;
      const previousCode = selectedDatasourceCodeRef.current;
      const currentZone = activeZoneRef.current;

      if (previousCode === normalizedCode) {
        setSelectedDatasourceCode(normalizedCode);
        return;
      }

      if (currentZone !== 'kg') {
        zoneCacheRef.current[getZoneCacheKey(currentZone, previousCode)] = framesRef.current;
      }
      selectedDatasourceCodeRef.current = normalizedCode;
      setSelectedDatasourceCode(normalizedCode);

      if (currentZone !== 'kg') {
        setVisibleFrames([], {
          zone: currentZone,
          datasourceCode: normalizedCode,
          cache: false,
        });
        void handleZoneChange(currentZone, { forceReload: true });
      }
    },
    [handleZoneChange, setVisibleFrames],
  );

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
      const queryLabel = t('kgBrowser.searchQueryLabel', { query: result.query });

      const newFrames: QueryFrame[] = components.map((comp, index) => {
        // Build per-component rows/columns so each card's table view
        // only shows the nodes belonging to that component.
        const { rows, columns } = buildRowsFromNodes(comp.nodes);
        return {
          id: `frame-${baseTime}-${index}-${Math.random().toString(16).slice(2)}`,
          query: queryLabel,
          subTitle: total > 1 ? `#${index + 1} / ${total}` : undefined,
          timestamp: baseTime,
          activeTab: 'graph' as ViewTab,
          result: comp,
          rows,
          columns,
          meta: {
            query: queryLabel,
            totalRecords: allNodes.length,
            queryType: 'subgraph_search',
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
    [setVisibleFrames, t],
  );

  /** Locate a node (clicked in the sidebar search results) in the canvas.
   *  A monotonic token lets repeated clicks on the same node re-trigger the
   *  focus effect inside the owning result frame. */
  const [locateTarget, setLocateTarget] = useState<{ key: string; token: number } | null>(null);
  const handleNodeLocate = useCallback((nodeKey: string) => {
    setLocateTarget((prev) => ({ key: nodeKey, token: (prev?.token ?? 0) + 1 }));
  }, []);

  const handleGraphViewChange = useCallback(
    (zone: GraphZone) => {
      setActiveView('graph');
      void handleZoneChange(zone);
    },
    [handleZoneChange],
  );

  return (
    <div className={styles.pageWrap}>
      {orgCrumbs.length > 0 && <OrgBreadcrumb items={orgCrumbs} />}
      <div className={`${styles.page} context-page-surface`}>
        {/* Left sidebar */}
        {activeView === 'graph' && (
          <DatabaseSidebar
            showDatabaseSelector={activeZone !== 'kg'}
            activeZone={activeZone}
            mgOnly={isGlobalMgView}
            targetOwnerId={targetOwnerId}
            onLabelClick={handleLabelClick}
            onRelTypeClick={handleRelTypeClick}
            onDatasourceCodeChange={handleDatabaseChange}
            onSubgraphSearch={handleSubgraphSearch}
            onNodeLocate={handleNodeLocate}
          />
        )}

        {/* Main content area */}
        <div className={styles.mainContent}>
          {/* View tabs bar — UG CRUD plus the graph layers; the global MG entry
            narrows this to the single MG tab. */}
          <div className={styles.zoneTabsBar} role="tablist">
            {!isGlobalMgView && (
              <Tooltip title={t('kgBrowser.zone_ug_desc')}>
                <div
                  className={`${styles.zoneTab} ${activeView === 'ug' ? styles.zoneTabActive : ''}`}
                  role="tab"
                  tabIndex={0}
                  aria-selected={activeView === 'ug'}
                  onClick={() => setActiveView('ug')}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setActiveView('ug');
                    }
                  }}
                >
                  <span className={styles.zoneTabKey}>UG</span>
                  <span className={styles.zoneTabName}>{t('kgBrowser.zone_ug')}</span>
                </div>
              </Tooltip>
            )}
            {visibleZoneTabs.map((zt) => {
              const editableText =
                zt.key !== 'mg' && zt.editableLabels.length > 0
                  ? `${t('kgBrowser.editableRange')}: ${zt.editableLabels.join(', ')}`
                  : t('kgBrowser.allReadonly');
              const readonlyText =
                zt.readonlyLabels.length > 0
                  ? `${t('kgBrowser.readonlyRange')}: ${zt.readonlyLabels.join(', ')}`
                  : '';
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
                    className={`${styles.zoneTab} ${activeView === 'graph' && activeZone === zt.key ? styles.zoneTabActive : ''}`}
                    role="tab"
                    tabIndex={0}
                    aria-selected={activeView === 'graph' && activeZone === zt.key}
                    onClick={() => handleGraphViewChange(zt.key)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        handleGraphViewChange(zt.key);
                      }
                    }}
                  >
                    <span className={styles.zoneTabKey}>{zt.key.toUpperCase()}</span>
                    <span className={styles.zoneTabName}>
                      {t(`kgBrowser.zone_${zt.key}`)}
                      {zt.key === 'mg'
                        ? ` (${t('common.readOnly', { defaultValue: '只读' })})`
                        : ''}
                    </span>
                  </div>
                </Tooltip>
              );
            })}
          </div>

          {activeView === 'ug' ? (
            <UserGraphPanel
              initialTargetUserId={browsingMemberId ?? (userId || undefined)}
              initialMemoryQuery={initialMemoryQuery}
            />
          ) : (
            <>
              {/* Userspace MG sync bar — own userspace only. member default view
            and ws_admin「我的图」(memberId=self) may pull; the admin global
            MG entry and other-member drilldown must not. */}
              {activeZone === 'mg' &&
                (!authEnabled || capabilities.includes('mg_userspace:sync')) &&
                (!authEnabled ||
                  (!isAdmin && !browsingMemberId) ||
                  browsingMemberId === userId) && <MgSyncBar onGraphChanged={handleRetry} />}

              {scopeHint && <div className={styles.scopeHint}>{scopeHint}</div>}

              {/* Scrollable frames container */}
              <div className={styles.framesContainer}>
                {pageLoading && frames.length > 0 && (
                  <div className={styles.framesLoadingOverlay}>
                    <Spin size="large" />
                    <span className={styles.framesLoadingText}>{t('kgBrowser.queryRunning')}</span>
                  </div>
                )}
                {pageLoading && frames.length === 0 ? (
                  <div className={styles.framesLoading}>{t('kgBrowser.queryRunning')}</div>
                ) : frames.length === 0 ? (
                  <div className={styles.framesInitial}>
                    <div className={styles.framesInitialQueryBar}>
                      <span className={styles.frameQueryPrefix}>{queryPrefix}</span>
                      <Input
                        value={initialQuery}
                        onChange={(e) => setInitialQuery(e.target.value)}
                        onPressEnter={() => handleExecuteQuery(initialQuery)}
                        disabled={isQueryDisabled}
                        className={styles.frameQueryInput}
                        variant="borderless"
                        size="small"
                        placeholder={t('kgBrowser.cypherPlaceholder')}
                      />
                      <Button
                        type="primary"
                        icon={<CaretRightOutlined />}
                        size="small"
                        onClick={() => handleExecuteQuery(initialQuery)}
                        loading={pageLoading}
                        disabled={pageLoading || isQueryDisabled}
                        className={styles.frameExecuteBtn}
                      />
                    </div>
                    {cypherError && (
                      <div className={styles.frameErrorPanel}>
                        <WarningOutlined className={styles.frameErrorIcon} />
                        <span className={styles.frameErrorText}>{cypherError}</span>
                        <CloseOutlined
                          className={styles.frameErrorClose}
                          onClick={() => setCypherError(null)}
                        />
                      </div>
                    )}
                    <div className={styles.framesInitialEmpty}>
                      <Empty
                        description={t('kgBrowser.noQueryResultsYet')}
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                      >
                        <Button
                          type="primary"
                          icon={<ReloadOutlined />}
                          onClick={handleRetry}
                          loading={pageLoading}
                        >
                          {t('kgBrowser.retry')}
                        </Button>
                      </Empty>
                    </div>
                  </div>
                ) : (
                  frames.map((frame) => (
                    <ResultFrame
                      key={frame.id}
                      frame={frame}
                      readOnly={activeZone === 'mg' || !canEditOwnKG || isDrilldownReadOnly}
                      queryDisabled={isQueryDisabled}
                      targetOwnerId={targetOwnerId}
                      queryPrefix={queryPrefix}
                      onRetryGraph={handleRetry}
                      onTabChange={handleTabChange}
                      onClose={handleCloseFrame}
                      onNodeUpdate={handleNodeUpdate}
                      onEdgeUpdate={handleEdgeUpdate}
                      onExecuteQuery={handleExecuteQuery}
                      errorMessage={frameErrors[frame.id]}
                      onDismissError={(fid) =>
                        setFrameErrors((prev) => {
                          const next = { ...prev };
                          delete next[fid];
                          return next;
                        })
                      }
                      loading={!!frameLoadingIds[frame.id]}
                      focusNodeKey={locateTarget?.key ?? null}
                      focusNodeToken={locateTarget?.token ?? 0}
                    />
                  ))
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
