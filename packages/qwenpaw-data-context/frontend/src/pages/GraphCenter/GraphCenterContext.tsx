/** GraphCenter data context — real auth-driven scope.
 *  The viewer/owner comes from the logged-in session (authStore), never from
 *  a simulated switcher:
 *  - member: sees only their own userspace (server-enforced read_own_graph).
 *  - ws_admin / tenant_admin: the global viewpoint shows ONLY the active
 *    workspace's global MG (owner='__global__' + metadata layer, server-
 *    enforced read_global_mg — KG/TG are user-owned layers with no global
 *    counterpart); every workspace user's graphs (their four graphs:
 *    MG mirror + TG + KG + UG) are reviewed one user at a time via the
 *    `memberId` drilldown (server-enforced review_others_graph), including
 *    the admin's own userspace ("我的图"). A tenant_admin is always bound to
 *    the active workspace — switching workspaces switches both the global
 *    graph and their own per-workspace userspace.
 *  The member universe comes from the auth workspace bindings (so members
 *  without any graph data still appear), supplemented by UG User nodes for
 *  owners without a workspace binding (e.g. another tenant_admin). */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import { useDataSourceList, useDataSourceOptionsStore } from '@/store/dataSourceOptions';
import { fetchWorkspaceMembers } from '@/hooks/useWorkspaceMembers';
import { userGraphApi } from '@/services/userGraph';
import { useGraphDataset, type DatasetSource } from './dataset/useGraphDataset';
import type { GraphDb } from './dataset/graphDb';
import type { GraphDataset } from './types';
import { persistViewMode, resolveViewMode, type GraphViewMode } from './viewMode';

export interface GraphScope {
  /** Logged-in user id ("" when auth is disabled). */
  userId: string;
  username: string;
  workspaceId: string;
  workspaceName: string;
  /** ws_admin / tenant_admin can review others' graphs. */
  isAdmin: boolean;
  authEnabled: boolean;
  capabilities: string[];
}

/** One drilldown target in the viewer switcher (a workspace user). */
export interface GraphMemberOption {
  id: string;
  username: string;
}

interface GraphCenterContextValue {
  scope: GraphScope;
  /** Admin drilldown target (member user id), empty = own/global scope. */
  memberId: string;
  setMemberId: (id: string) => void;
  /** Workspace member list for the admin drilldown chips (empty for members). */
  memberOptions: GraphMemberOption[];
  /** False while the admin member universe is still being resolved. */
  membersLoaded: boolean;
  /** MG datasource narrowing selected in the global page chips. */
  datasourceId: string;
  setDatasourceId: (id: string) => void;
  /** Datasource id → display name (management list first, graph fallback). */
  dsNames: Record<string, string>;
  viewMode: GraphViewMode;
  setViewMode: (mode: GraphViewMode) => void;
  db: GraphDb;
  dataset: GraphDataset;
  loading: boolean;
  error: string | null;
  source: DatasetSource;
  reload: () => void;
}

const GraphCenterContext = createContext<GraphCenterContextValue | null>(null);

export function GraphCenterProvider({ children }: { children: ReactNode }) {
  const userId = useAuthStore((s) => s.userId);
  const username = useAuthStore((s) => s.username);
  const displayName = useAuthStore((s) => s.displayName);
  const workspaceId = useAuthStore((s) => s.activeWorkspaceId);
  const workspaceName = useAuthStore((s) => s.activeWorkspaceName);
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const capabilities = useAuthStore((s) => s.capabilities);

  const [searchParams, setSearchParams] = useSearchParams();
  /** State is authoritative; the URL param is a mirror for deep links and
   *  shareability. Deep-link reads happen once at mount (e.g. arriving from
   *  the graph-management member list via ?member=<id>). */
  const [memberId, setMemberIdState] = useState(() => searchParams.get('member') || '');
  const [datasourceId, setDatasourceId] = useState('all');

  /** Keep the URL aligned with the viewpoint state (one-way, state → URL).
   *  Tab switches and intra-center navigations drop ?member= from the URL;
   *  this puts it back so the selected viewpoint survives page changes.
   *  (The inverse direction — URL driving state — would reset the drilldown
   *  on every navigate() that doesn't carry the param.) */
  const urlMode = searchParams.get('mode');
  const rawRequested = searchParams.get('cypher') === 'raw';
  const viewMode = resolveViewMode({
    urlMode: rawRequested ? 'dev' : urlMode,
    isAdmin,
    authEnabled,
  });

  useEffect(() => {
    if (urlMode === 'dev' || urlMode === 'biz') persistViewMode(urlMode);
  }, [urlMode]);

  useEffect(() => {
    if (!rawRequested || urlMode === 'dev') return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('mode', 'dev');
        next.set('cypher', 'raw');
        return next;
      },
      { replace: true },
    );
    persistViewMode('dev');
  }, [rawRequested, urlMode, setSearchParams]);

  useEffect(() => {
    const urlMember = searchParams.get('member') || '';
    if (urlMember === memberId) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (memberId) next.set('member', memberId);
        else next.delete('member');
        return next;
      },
      { replace: true },
    );
  }, [memberId, searchParams, setSearchParams]);

  const setMemberId = useCallback((id: string) => {
    setMemberIdState(id);
  }, []);

  const setViewMode = useCallback(
    (mode: GraphViewMode) => {
      persistViewMode(mode);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set('mode', mode);
          if (mode === 'biz') next.delete('cypher');
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const scope = useMemo<GraphScope>(
    () => ({
      userId,
      // Display name first; falls back to username when absent.
      username: displayName || username,
      workspaceId,
      workspaceName,
      isAdmin,
      authEnabled,
      capabilities,
    }),
    [userId, displayName, username, workspaceId, workspaceName, isAdmin, authEnabled, capabilities],
  );

  /** Admin drilldown options: auth workspace bindings are the authoritative
   *  universe (members appear before writing any graph data); UG User nodes
   *  supplement owners without a binding. Auth disabled → no session, the UG
   *  list is the only source (local-dev/demo behavior). */
  const isAdminView = isAdmin || !authEnabled;
  const [memberOptions, setMemberOptions] = useState<GraphMemberOption[]>([]);
  const [membersLoaded, setMembersLoaded] = useState(!isAdminView);
  useEffect(() => {
    if (!isAdminView) {
      setMemberOptions([]);
      setMembersLoaded(true);
      return;
    }
    let cancelled = false;
    setMembersLoaded(false);
    (async () => {
      const byId = new Map<string, GraphMemberOption>();
      if (authEnabled && workspaceId) {
        try {
          (await fetchWorkspaceMembers(workspaceId)).forEach((m) => {
            byId.set(m.id, { id: m.id, username: m.displayName || m.username });
          });
        } catch {
          // Auth listing unavailable — fall through to the UG supplement.
        }
      }
      try {
        const p = await userGraphApi.listUsers({ page: 1, pageSize: 50 });
        p.items.forEach((u) => {
          if (byId.has(u.user_id)) return;
          byId.set(u.user_id, {
            id: u.user_id,
            username: u.profile?.display_name || u.user_id.slice(0, 10),
          });
        });
      } catch {
        // UG listing is best-effort only.
      }
      if (cancelled) return;
      // Self is represented by the dedicated "我的图" chip, not a member chip.
      setMemberOptions([...byId.values()].filter((m) => m.id !== userId));
      setMembersLoaded(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [isAdminView, authEnabled, workspaceId, userId]);

  /** Display name for the single-user view owner (drilldown target or self);
   *  UG anchor labels prefer the auth username over the raw id prefix. */
  const ownerName = useMemo(() => {
    const target = memberId || userId;
    if (!target) return '';
    if (target === userId) return displayName || username || userId.slice(0, 10);
    return memberOptions.find((m) => m.id === target)?.username || '';
  }, [memberId, userId, displayName, username, memberOptions]);

  const { dataset, db, loading, error, source, reload } = useGraphDataset({
    ownerId: memberId || undefined,
    workspaceId,
    isAdmin,
    authEnabled,
    selfId: userId,
    ownerName,
  });

  /** Datasource display names: the management list is the authority (that is
   *  where users name datasources); the graph's DataSource anchor labels
   *  cover demo/offline datasets. Chip VALUES stay raw datasource ids. */
  const fetchDsOptions = useDataSourceOptionsStore((s) => s.fetchOptions);
  useEffect(() => {
    void fetchDsOptions();
  }, [fetchDsOptions]);
  const dsItems = useDataSourceList();
  const dsNames = useMemo(() => {
    const m = db.datasourceNames();
    dsItems.forEach((it) => {
      if (it.datasource_id && it.datasource_name) m[it.datasource_id] = it.datasource_name;
    });
    return m;
  }, [db, dsItems]);

  const value = useMemo(
    () => ({
      scope,
      memberId,
      setMemberId,
      memberOptions,
      membersLoaded,
      datasourceId,
      setDatasourceId,
      dsNames,
      viewMode,
      setViewMode,
      db,
      dataset,
      loading,
      error,
      source,
      reload,
    }),
    [
      scope,
      memberId,
      setMemberId,
      memberOptions,
      membersLoaded,
      datasourceId,
      dsNames,
      viewMode,
      setViewMode,
      db,
      dataset,
      loading,
      error,
      source,
      reload,
    ],
  );

  return <GraphCenterContext.Provider value={value}>{children}</GraphCenterContext.Provider>;
}

export function useGraphCenter(): GraphCenterContextValue {
  const ctx = useContext(GraphCenterContext);
  if (!ctx) throw new Error('useGraphCenter must be used inside GraphCenterProvider');
  return ctx;
}
