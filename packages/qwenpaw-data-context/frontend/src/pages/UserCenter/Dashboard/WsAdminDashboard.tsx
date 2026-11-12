import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { TeamOutlined, RightOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import type { User, Workspace } from '@/auth';
import { useAuthStore } from '@/store/authStore';

type UserItem = User;
type WorkspaceItem = Workspace;

interface WsAdminDashboardProps {
  /** In-page action provided by the console; falls back to a route jump when absent. */
  onViewMembers?: () => void;
}

const FONT_FAMILY = "'Inter', 'Noto Sans SC', sans-serif";

const heroStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 20,
  background: '#fff',
  border: '1px solid rgba(5, 5, 5, 0.08)',
  borderRadius: 12,
  padding: '20px 24px',
  boxShadow: '0 2px 10px rgba(31, 35, 41, 0.04)',
  fontFamily: FONT_FAMILY,
};

const avatarStyle: CSSProperties = {
  width: 56,
  height: 56,
  borderRadius: 16,
  background: '#eef2ff',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 24,
  fontWeight: 700,
  color: '#6366f1',
  flexShrink: 0,
};

const pillBaseStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '2px 10px',
  borderRadius: 999,
  fontSize: 12,
  lineHeight: '20px',
  whiteSpace: 'nowrap',
};

const statusPillStyle = (active: boolean): CSSProperties => ({
  ...pillBaseStyle,
  background: active ? 'rgba(16, 185, 129, 0.08)' : '#f8fafc',
  color: active ? '#059669' : '#94a3b8',
});

const rolePillStyle: CSSProperties = {
  ...pillBaseStyle,
  background: 'rgba(99, 102, 241, 0.08)',
  color: '#6366f1',
};

const statBlockStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  cursor: 'pointer',
  padding: '4px 8px',
  borderRadius: 12,
};

export default function WsAdminDashboard({ onViewMembers }: WsAdminDashboardProps) {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const navigate = useNavigate();
  const { activeWorkspaceId, activeWorkspaceName, activeWorkspaceRole } = useAuthStore();
  const [users, setUsers] = useState<UserItem[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);

  const gotoMembers = onViewMembers ?? (() => navigate('/user-center/console?tab=members'));

  useEffect(() => {
    if (!activeWorkspaceId) return;
    authFetch<{ users: UserItem[] }>(
      `/api/users?workspace_id=${encodeURIComponent(activeWorkspaceId)}`,
    )
      .then((r) => setUsers(r.users || []))
      .catch(() => {});
    authFetch<{ workspaces: WorkspaceItem[] }>('/api/workspaces')
      .then((r) => setWorkspaces(r.workspaces || []))
      .catch(() => {});
  }, [activeWorkspaceId]);

  const currentWs = workspaces.find((w) => w.id === activeWorkspaceId);
  const wsName = currentWs?.name || activeWorkspaceName || '-';
  const isActive = currentWs?.status === 'active';

  return (
    <div style={{ padding: '4px 0 24px' }}>
      <div style={heroStyle}>
        {/* Workspace identity */}
        <div style={avatarStyle}>{wsName[0]?.toUpperCase() || 'W'}</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: '#1e293b' }}>{wsName}</span>
            {currentWs && (
              <span style={statusPillStyle(isActive)}>
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: isActive ? '#10b981' : '#cbd5e1',
                  }}
                />
                {t(`status.${currentWs.status}`)}
              </span>
            )}
            <span style={rolePillStyle}>{t(`role.${activeWorkspaceRole}`)}</span>
          </div>
          <span style={{ fontSize: 12, color: '#94a3b8' }}>{t('dashboard.currentWorkspace')}</span>
        </div>

        <div style={{ flex: 1 }} />

        {/* Member count — clickable, jumps to the members tab */}
        <div style={{ width: 1, alignSelf: 'stretch', background: 'rgba(226, 232, 240, 0.8)' }} />
        <div
          style={statBlockStyle}
          onClick={gotoMembers}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = '#f8fafc';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'transparent';
          }}
        >
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: '50%',
              background: '#eef2ff',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#6366f1',
              fontSize: 18,
            }}
          >
            <TeamOutlined />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <span style={{ fontSize: 22, fontWeight: 700, lineHeight: '26px', color: '#1e293b' }}>
              {users.length}
            </span>
            <span style={{ fontSize: 12, color: '#64748b', whiteSpace: 'nowrap' }}>
              {t('dashboard.wsMembers')}
            </span>
          </div>
          <RightOutlined style={{ fontSize: 12, color: '#cbd5e1', marginLeft: 4 }} />
        </div>
      </div>
    </div>
  );
}
