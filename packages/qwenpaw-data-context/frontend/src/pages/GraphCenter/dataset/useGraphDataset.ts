/** Dataset loading — real scoped backend first, demo snapshot as offline fallback.
 *  Tenant/workspace/owner scope is enforced SERVER-side (graph_access); this
 *  hook only selects the owner from the real session:
 *  - member          → own userspace (read_own_graph, owner=self)
 *  - admin, no owner → the active workspace's GLOBAL MG only
 *                      (read_global_mg: owner='__global__' + metadata layer;
 *                      KG/TG are user-owned with no global counterpart);
 *                      users' graphs never mix into the global canvas — they
 *                      are reviewed per user via the `ownerId` drilldown
 *  - admin + ownerId → that user's userspace (review_others_graph),
 *                      including the admin's own ("我的图")
 *  The UG layer follows the same viewpoint: userspace views show that single
 *  user's anchor + memories (the anchor renders even when the UG is still
 *  empty); the admin global view keeps UG empty by design. Auth disabled
 *  keeps the legacy all-users listing for the offline/demo flow.
 *  Datasource narrowing is applied client-side on the loaded dataset (same
 *  code path for the API and the offline demo snapshot). When the backend is
 *  unreachable (network error) the static demo snapshot is used and clearly
 *  labeled via source === "demo" — both view modes use the same non-TG dataset. */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { explorerApi } from '@/services/cmgraph';
import { ApiRequestError } from '@/services/apiError';
import { userGraphApi } from '@/services/userGraph';
import type { UserGraphMemory, UserGraphUser } from '@/services/userGraph/types';
import { datasetFromApiGraph, datasetFromUserGraph, mergeDatasets } from './apiAdapter';
import { demoDataset } from './graphData';
import { createGraphDb, type GraphDb } from './graphDb';
import type { GraphDataset, GraphEdge, GraphNode } from '../types';

interface UseGraphDatasetOptions {
  /** Admin drilldown into one member's userspace; empty = own/global scope. */
  ownerId?: string;
  /** Tenant/workspace identifier for meta only. */
  workspaceId: string;
  /** ws_admin / tenant_admin session — their default view is the global graph. */
  isAdmin: boolean;
  authEnabled: boolean;
  /** Logged-in user id ("" when auth is disabled). */
  selfId: string;
  /** Display name of the single-user view owner (drilldown target or self). */
  ownerName: string;
}

export type DatasetSource = 'api' | 'demo';

interface DatasetState {
  dataset: GraphDataset;
  db: GraphDb;
  loading: boolean;
  error: string | null;
  source: DatasetSource;
  reload: () => void;
}

interface UserGraphPart {
  part: { nodes: GraphNode[]; edges: GraphEdge[] };
}

const EMPTY_UG_PART: UserGraphPart = { part: { nodes: [], edges: [] } };

/** Legacy all-users UG listing — auth-disabled local dev/demo only. */
async function fetchAllUsersGraphPart(ownerId?: string): Promise<UserGraphPart> {
  try {
    const { items } = await userGraphApi.listUsers({ page: 1, pageSize: 10 });
    const users = ownerId ? items.filter((u: UserGraphUser) => u.user_id === ownerId) : items;
    const memoriesByUser: Record<string, UserGraphMemory[]> = {};
    await Promise.all(
      users.map(async (u: UserGraphUser) => {
        try {
          const page = await userGraphApi.listMemories(u.user_id, { page: 1, pageSize: 10 });
          memoriesByUser[u.user_id] = page.items;
        } catch {
          memoriesByUser[u.user_id] = [];
        }
      }),
    );
    return {
      part: datasetFromUserGraph(users, memoriesByUser),
    };
  } catch {
    return EMPTY_UG_PART;
  }
}

/** One user's UG for the userspace views: the anchor node is synthesized
 *  from the session/member identity so it renders even before the user has
 *  written any memory (a User node may not exist in the graph yet). */
async function fetchSingleUserGraphPart(
  ownerId: string,
  ownerName: string,
): Promise<UserGraphPart> {
  let user: UserGraphUser = {
    workspace_id: '',
    user_id: ownerId,
    profile: null,
    status: 'active',
    created_at: null,
    updated_at: null,
  };
  try {
    const fetched = await userGraphApi.getUser(ownerId);
    if (fetched) user = fetched;
  } catch {
    // Profile lookup is best-effort; keep the synthesized anchor.
  }
  let memories: UserGraphMemory[] = [];
  try {
    memories = (await userGraphApi.listMemories(ownerId, { page: 1, pageSize: 10 })).items;
  } catch {
    memories = [];
  }
  const names = ownerName ? { [ownerId]: ownerName } : {};
  return {
    part: datasetFromUserGraph([user], { [ownerId]: memories }, names),
  };
}

const EMPTY_DATASET: GraphDataset = {
  nodes: [],
  edges: [],
  meta: {
    workspace: '',
    source: 'pending',
    nodeCount: 0,
    edgeCount: 0,
    byLayer: { MG: 0, TG: 0, KG: 0, UG: 0 },
  },
};

export function useGraphDataset({
  ownerId,
  workspaceId,
  isAdmin,
  authEnabled,
  selfId,
  ownerName,
}: UseGraphDatasetOptions): DatasetState {
  const [dataset, setDataset] = useState<GraphDataset>(EMPTY_DATASET);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<DatasetSource>('api');
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDataset(EMPTY_DATASET);
    setSource('api');
    (async () => {
      try {
        const graph = await explorerApi.getGlobalGraph({
          max_nodes: 800,
          max_edges: 3000,
          domain_roots_only: false,
          skeleton: false,
          zone_mode: 'all',
          ...(ownerId ? { owner_id: ownerId } : {}),
        });
        // UG follows the viewpoint: single-user anchor in userspace views,
        // empty in the admin global view, legacy all-users when auth is off.
        const ug = !authEnabled
          ? await fetchAllUsersGraphPart(ownerId || undefined)
          : ownerId
            ? await fetchSingleUserGraphPart(ownerId, ownerName)
            : isAdmin || !selfId
              ? EMPTY_UG_PART
              : await fetchSingleUserGraphPart(selfId, ownerName);
        if (cancelled) return;
        const apiPart = datasetFromApiGraph(graph.nodes, graph.edges);
        const ds = mergeDatasets(workspaceId, 'api', [apiPart, ug.part]);
        setDataset(ds);
        setSource('api');
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiRequestError && err.status === 0) {
          setDataset(mergeDatasets(workspaceId, 'demo', [demoDataset()]));
          setSource('demo');
          setError('BACKEND_UNREACHABLE');
        } else {
          setError(
            err instanceof ApiRequestError &&
              (err.code === 'user_space_uninitialized' || err.code === 'user_space_initializing')
              ? err.code
              : err instanceof Error
                ? err.message
                : String(err),
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ownerId, workspaceId, reloadTick, isAdmin, authEnabled, selfId, ownerName]);

  useEffect(() => {
    if (error !== 'user_space_uninitialized' && error !== 'user_space_initializing') return;
    const timer = setTimeout(() => setReloadTick((v) => v + 1), 6000);
    return () => clearTimeout(timer);
  }, [error]);

  const db = useMemo(() => createGraphDb(dataset), [dataset]);
  const reload = useCallback(() => setReloadTick((v) => v + 1), []);

  return { dataset, db, loading, error, source, reload };
}
