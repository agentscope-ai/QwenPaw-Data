import { message } from '@/design';
import { switchWorkspace } from '@/auth';
import { useAuthStore } from '@/store/authStore';
import { getContextBasePath } from '@/services/consoleConfig';

export const GRAPH_MANAGEMENT_PATH = '/graph-management';

export function workspaceDetailPath(workspaceId: string): string {
  return `${GRAPH_MANAGEMENT_PATH}/workspace/${encodeURIComponent(workspaceId)}`;
}

/** Graph Center global graph — the unified visualization portal. Admin drill
 *  down into one member's userspace via the `member` param (server-enforced
 *  review_others_graph scope). */
export function graphCenterGlobalPath(params?: { memberId?: string }): string {
  const query = new URLSearchParams();
  if (params?.memberId) query.set('member', params.memberId);
  const qs = query.toString();
  return `/graph-center/global${qs ? `?${qs}` : ''}`;
}

export function explorePath(params?: {
  memberId?: string;
  memberName?: string;
  zone?: string;
}): string {
  const query = new URLSearchParams();
  if (params?.memberId) query.set('memberId', params.memberId);
  if (params?.memberName) query.set('memberName', params.memberName);
  if (params?.zone) query.set('zone', params.zone);
  const qs = query.toString();
  return `${GRAPH_MANAGEMENT_PATH}/explore${qs ? `?${qs}` : ''}`;
}

/** Switch the session to another workspace, then hard-navigate to restore
 *  the drill-down position after the full reload (workspaces are physically
 *  isolated Neo4j instances, so a session switch + reload is required). */
export async function switchWorkspaceAndGo(
  workspaceId: string,
  targetPath: string,
  errorText: string,
): Promise<void> {
  try {
    await switchWorkspace(workspaceId);
    await useAuthStore.getState().refresh();
    if (useAuthStore.getState().activeWorkspaceId !== workspaceId) {
      throw new Error(errorText);
    }
    window.location.href = `${getContextBasePath().replace(/\/$/, '')}${targetPath}`;
  } catch (err) {
    console.error('Failed to switch workspace', err);
    message.error(err instanceof Error ? err.message : errorText);
    await useAuthStore
      .getState()
      .refresh()
      .catch(() => undefined);
    throw err;
  }
}
