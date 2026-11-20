import React, { useCallback, useEffect, useState } from 'react';
import { Avatar, Button, Card, Empty, Skeleton, Space, Switch, Tag, message } from 'antd';
import {
  AppstoreOutlined,
  PlusOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  UserAddOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import type { Tenant, User, Workspace } from '@/auth';
import StatCard from '@/pages/UserCenter/components/StatCard';
import { getErrorMessage } from '@/services/apiError';
import styles from './TenantAdminDashboard.module.css';

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type UserItem = User & {
  last_login_at?: string | null;
};
type WorkspaceItem = Workspace;
type TenantItem = Tenant;

interface TenantAdminDashboardProps {
  /** In-page actions provided by the console; fall back to route jumps when absent. */
  onInviteUser?: () => void;
  onCreateWorkspace?: () => void;
  onViewMembers?: () => void;
  onViewRoles?: () => void;
  onViewWorkspaces?: () => void;
}

/* ------------------------------------------------------------------ */
/*  Role tag colors                                                    */
/* ------------------------------------------------------------------ */

const ROLE_TAG_COLOR: Record<string, string> = {
  tenant_admin: '#ef4444',
  ws_admin: '#f59e0b',
  member: '#1677ff',
};

function formatRecentLogin(value: string | null | undefined): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/* ------------------------------------------------------------------ */
/*  Styles                                                             */
/* ------------------------------------------------------------------ */

const PAGE_STYLE: React.CSSProperties = {
  padding: '4px 0 24px',
};

const SECTION_TITLE: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  color: 'var(--qwenpaw-data-color-text)',
  marginBottom: 16,
  fontFamily: "'Inter', 'Noto Sans SC', sans-serif",
};

const STAT_GRID: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
  gap: 16,
  marginBottom: 32,
};

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

const TenantAdminDashboard: React.FC<TenantAdminDashboardProps> = ({
  onInviteUser,
  onCreateWorkspace,
  onViewMembers,
  onViewRoles,
  onViewWorkspaces,
}) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const navigate = useNavigate();
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  const [users, setUsers] = useState<UserItem[]>([]);
  const [tenant, setTenant] = useState<TenantItem | null>(null);
  const [savingJoinOpen, setSavingJoinOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const gotoMembers = onViewMembers ?? (() => navigate('/user-center/console?tab=members'));
  const gotoRoles = onViewRoles ?? gotoMembers;
  const gotoWorkspaces =
    onViewWorkspaces ?? (() => navigate('/user-center/console?tab=workspaces'));
  const inviteUser = onInviteUser ?? gotoMembers;
  const createWorkspace = onCreateWorkspace ?? gotoWorkspaces;

  const loadData = useCallback(async () => {
    try {
      const [wsResult, userResult, tenantResult] = await Promise.all([
        authFetch<{ workspaces: WorkspaceItem[] }>('/api/workspaces'),
        authFetch<{ users: UserItem[] }>('/api/users'),
        // Scoped to the caller's own tenant, so the list holds at most one row.
        authFetch<{ tenants: TenantItem[] }>('/api/tenants'),
      ]);
      setWorkspaces(wsResult.workspaces ?? []);
      setUsers(userResult.users ?? []);
      setTenant(tenantResult.tenants?.[0] ?? null);
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('common.loadFailed')));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleJoinOpenChange = async (checked: boolean) => {
    if (!tenant) return;
    setSavingJoinOpen(true);
    try {
      const result = await authFetch<{ tenant: TenantItem }>(`/api/tenants/${tenant.id}`, {
        method: 'PUT',
        body: JSON.stringify({ join_open: checked }),
      });
      setTenant(result.tenant);
      message.success(t('common.editSuccess'));
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setSavingJoinOpen(false);
    }
  };

  /* Computed stats */
  const totalWorkspaces = workspaces.length;
  const totalUsers = users.length;
  const activeUsers = users.filter((u) => u.status === 'active').length;
  const roleBindingCount = users.reduce((sum, u) => sum + (u.role_bindings?.length ?? 0), 0);

  const recentUsers = users.slice(-5).reverse();

  return (
    <div style={PAGE_STYLE}>
      {/* Stat cards */}
      {loading ? (
        <div style={STAT_GRID}>
          {[1, 2, 3, 4].map((k) => (
            <Skeleton.Node key={k} active style={{ width: '100%', height: 88 }} />
          ))}
        </div>
      ) : (
        <div style={STAT_GRID}>
          <StatCard
            icon={<AppstoreOutlined />}
            label={t('dashboard.workspaces')}
            value={totalWorkspaces}
            onClick={gotoWorkspaces}
          />
          <StatCard
            icon={<TeamOutlined />}
            label={t('dashboard.totalUsers')}
            value={totalUsers}
            onClick={gotoMembers}
          />
          <StatCard
            icon={<TeamOutlined />}
            label={t('dashboard.activeUsers')}
            value={activeUsers}
            color="var(--qwenpaw-data-color-success, #16a34a)"
            onClick={gotoMembers}
          />
          <StatCard
            icon={<SafetyCertificateOutlined />}
            label={t('dashboard.roleBindings')}
            value={roleBindingCount}
            onClick={gotoRoles}
          />
        </div>
      )}

      {/* Quick Actions */}
      <h2 style={SECTION_TITLE}>{t('dashboard.quickActions')}</h2>
      <Space style={{ marginBottom: 32 }}>
        <Button
          type="primary"
          icon={<UserAddOutlined />}
          onClick={inviteUser}
          style={{ borderRadius: 12 }}
        >
          {t('dashboard.inviteUser')}
        </Button>
        <Button
          type="default"
          icon={<PlusOutlined />}
          onClick={createWorkspace}
          style={{ borderRadius: 12 }}
        >
          {t('dashboard.createWorkspace')}
        </Button>
      </Space>

      {/* Tenant visibility: the only tenant_admin entry for the tenant-level switch. */}
      {tenant ? (
        <Card style={{ borderRadius: 12, marginBottom: 32 }}>
          <Space align="start" size={12}>
            <Switch
              checked={Boolean(tenant.join_open)}
              loading={savingJoinOpen}
              onChange={handleJoinOpenChange}
            />
            <div>
              <div style={{ fontWeight: 500, color: '#1e293b' }}>{t('tenant.joinOpen')}</div>
              <div style={{ color: '#ff4d4f', fontSize: 12 }}>{t('tenant.joinOpenWarning')}</div>
            </div>
          </Space>
        </Card>
      ) : null}

      {/* Recent Users */}
      <h2 style={SECTION_TITLE}>{t('dashboard.recentUsers')}</h2>
      {loading ? (
        <div className={styles.recentUserGrid}>
          {[1, 2, 3, 4, 5].map((key) => (
            <Skeleton.Node key={key} active className={styles.userCardSkeleton} />
          ))}
        </div>
      ) : recentUsers.length > 0 ? (
        <div className={styles.recentUserGrid}>
          {recentUsers.map((user) => {
            const primaryBinding = user.role_bindings?.[0];
            const role = primaryBinding?.role ?? 'member';
            const roleColor = ROLE_TAG_COLOR[role] ?? '#64748b';
            const displayName = user.display_name || user.username;
            return (
              <button key={user.id} className={styles.userCard} type="button" onClick={gotoMembers}>
                <Avatar className={styles.userAvatar} style={{ backgroundColor: roleColor }}>
                  {displayName.slice(0, 1).toUpperCase()}
                </Avatar>
                <span className={styles.userCardBody}>
                  <span className={styles.userNameRow}>
                    <span className={styles.userName}>{displayName}</span>
                    <Tag color={roleColor}>{t(`role.${role}`)}</Tag>
                  </span>
                  <span className={styles.userDetailRow}>
                    <span className={styles.userDetailLabel}>{t('dashboard.lastLogin')}：</span>
                    <span className={styles.userDetailValue}>
                      {formatRecentLogin(user.last_login_at)}
                    </span>
                  </span>
                  <span className={styles.userDetailRow}>
                    <span className={styles.userDetailLabel}>
                      {t('dashboard.businessWorkspace')}：
                    </span>
                    <span className={styles.userDetailValue} title={primaryBinding?.scope_name}>
                      {primaryBinding?.scope_name || '-'}
                    </span>
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className={styles.emptyUsers}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />
        </div>
      )}
    </div>
  );
};

export default TenantAdminDashboard;
