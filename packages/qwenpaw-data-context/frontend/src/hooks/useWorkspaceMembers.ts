import { useEffect, useState } from 'react';
import { getAuthApiUrl } from '@/auth';

export interface WorkspaceMember {
  id: string;
  username: string;
  // Preferred display name; falls back to username when absent.
  displayName: string;
  /** Role bound to the queried workspace (e.g. ws_admin / member). */
  role?: string;
}

interface RawUser {
  id: string;
  username: string;
  display_name?: string;
  role_bindings?: Array<{ role: string; scope_type: string; scope_id: string }>;
}

/** Fetch the member list of one workspace from the auth service.
 *
 *  The auth `role_bindings` table is the authoritative member universe:
 *  every ws_admin/member bound to the workspace appears here even before
 *  they have written any graph data. (tenant_admins carry a tenant-scope
 *  binding instead of a workspace one, so they are not in this list.)
 */
export async function fetchWorkspaceMembers(workspaceId: string): Promise<WorkspaceMember[]> {
  const url = `${getAuthApiUrl()}/api/users?workspace_id=${encodeURIComponent(workspaceId)}`;
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data: { users?: RawUser[] } = await res.json();
  return (data.users || []).map((u) => ({
    id: u.id,
    username: u.username,
    displayName: u.display_name || u.username,
    role: u.role_bindings?.find((b) => b.scope_type === 'workspace' && b.scope_id === workspaceId)
      ?.role,
  }));
}

/** Hook wrapper around {@link fetchWorkspaceMembers} for page components. */
export function useWorkspaceMembers(workspaceId: string | undefined) {
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchWorkspaceMembers(workspaceId)
      .then((list) => {
        if (!cancelled) setMembers(list);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error('Failed to fetch workspace members:', err);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  return { members, loading, error };
}
