import React, { useCallback, useMemo, useState } from 'react';
import { Badge, Spin, Tabs } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/store/authStore';
import UsersTab from '@/pages/UserCenter/Members/UsersTab';
import AccessRequestsTab from '@/pages/UserCenter/Members/AccessRequestsTab';
import WorkspaceManagement from '@/pages/UserCenter/WorkspaceManagement';
import PlatformDashboard from '@/pages/UserCenter/Dashboard/PlatformDashboard';
import TenantAdminDashboard from '@/pages/UserCenter/Dashboard/TenantAdminDashboard';
import WsAdminDashboard from '@/pages/UserCenter/Dashboard/WsAdminDashboard';
import MemberHome from '@/pages/UserCenter/Dashboard/MemberHome';

type ConsoleTab = 'members' | 'access-requests' | 'workspaces';

const TAB_KEYS: readonly ConsoleTab[] = ['members', 'access-requests', 'workspaces'];

function isConsoleTab(value: string | null): value is ConsoleTab {
  return value !== null && (TAB_KEYS as readonly string[]).includes(value);
}

/**
 * User-center console: role-aware overview on top, management tabs below.
 */
const Console: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const { t: tRoot } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [inviteTick, setInviteTick] = useState(0);
  const [createWorkspaceTick, setCreateWorkspaceTick] = useState(0);
  const [pendingCount, setPendingCount] = useState(0);

  const role = useAuthStore((s) => s.role);
  const activeWorkspaceRole = useAuthStore((s) => s.activeWorkspaceRole);
  const initialized = useAuthStore((s) => s.initialized);

  const rawTab = searchParams.get('tab');
  const activeTab: ConsoleTab = isConsoleTab(rawTab) ? rawTab : 'members';

  const setTab = useCallback(
    (tab: ConsoleTab) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', tab);
        next.delete('user_id');
        next.delete('userId');
        return next;
      });
    },
    [setSearchParams],
  );

  /* ---- in-page actions wired into the overview area ---- */

  const handleViewMembers = useCallback(() => setTab('members'), [setTab]);
  const handleViewWorkspaces = useCallback(() => setTab('workspaces'), [setTab]);

  const handleInviteUser = useCallback(() => {
    setTab('members');
    setInviteTick((tick) => tick + 1);
  }, [setTab]);

  const handleCreateWorkspace = useCallback(() => {
    setTab('workspaces');
    setCreateWorkspaceTick((tick) => tick + 1);
  }, [setTab]);

  const tabItems = useMemo(
    () => [
      {
        key: 'members',
        label: t('members.tabUsers'),
        children: <UsersTab openInviteTick={inviteTick} />,
      },
      {
        key: 'access-requests',
        label: (
          <Badge count={pendingCount} offset={[10, -2]} size="small">
            {t('accessRequests.tab')}
          </Badge>
        ),
        // Mount eagerly: the badge is the only signal that work is waiting.
        forceRender: true,
        children: <AccessRequestsTab onPendingCountChange={setPendingCount} />,
      },
      {
        key: 'workspaces',
        label: tRoot('menu.ucWorkspaces'),
        children: <WorkspaceManagement openCreateTick={createWorkspaceTick} />,
      },
    ],
    [t, tRoot, inviteTick, createWorkspaceTick, pendingCount],
  );

  if (!initialized) {
    return (
      <div
        style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 320 }}
      >
        <Spin size="large" />
      </div>
    );
  }

  const effectiveRole = role === 'platform_admin' ? role : activeWorkspaceRole;

  /* Platform admins and members keep their overview-only view (no management tabs). */
  if (effectiveRole === 'platform_admin') {
    return <PlatformDashboard />;
  }
  if (effectiveRole !== 'tenant_admin' && effectiveRole !== 'ws_admin') {
    return <MemberHome />;
  }

  return (
    <div>
      {effectiveRole === 'tenant_admin' ? (
        <TenantAdminDashboard
          onInviteUser={handleInviteUser}
          onCreateWorkspace={handleCreateWorkspace}
          onViewMembers={handleViewMembers}
          onViewRoles={handleViewMembers}
          onViewWorkspaces={handleViewWorkspaces}
        />
      ) : (
        <WsAdminDashboard onViewMembers={handleViewMembers} />
      )}

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setTab(key as ConsoleTab)}
        items={tabItems}
        destroyInactiveTabPane={false}
      />
    </div>
  );
};

export default Console;
