import { Card, List, Space, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/store/authStore';
import PermissionMatrix from '@/pages/UserCenter/components/PermissionMatrix';

export default function MemberHome() {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const {
    username,
    displayName,
    activeWorkspaceId,
    activeWorkspaceName,
    activeWorkspaceRole,
    workspaces,
  } = useAuthStore();

  return (
    <div style={{ padding: '4px 0 24px' }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, color: '#1e293b', marginBottom: 8 }}>
        {t('dashboard.welcome', { name: displayName || username })}
      </h2>
      <p style={{ fontSize: 14, color: '#64748b', marginBottom: 32 }}>
        {t(`role.${activeWorkspaceRole}`)}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, marginBottom: 24 }}>
        <Card title={t('dashboard.currentWorkspace')}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div
              style={{
                width: 48,
                height: 48,
                borderRadius: 16,
                background: '#eef2ff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 20,
                fontWeight: 700,
                color: '#6366f1',
              }}
            >
              {(activeWorkspaceName || 'W')[0].toUpperCase()}
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 16, color: '#1e293b' }}>
                {activeWorkspaceName || '-'}
              </div>
              <Tag color="#10b981" style={{ marginTop: 4 }}>
                {t('status.active')}
              </Tag>
            </div>
          </div>
        </Card>

        <Card title={t('dashboard.accessibleWorkspaces')}>
          {workspaces.length > 0 ? (
            <List
              size="small"
              dataSource={workspaces}
              renderItem={(ws) => (
                <List.Item>
                  <Space>
                    <span style={{ color: '#1e293b' }}>{ws.name}</span>
                    {ws.id === activeWorkspaceId && (
                      <Tag color="#6366f1">{t('workspaceSwitcher.current')}</Tag>
                    )}
                  </Space>
                </List.Item>
              )}
            />
          ) : (
            <div style={{ color: '#94a3b8', textAlign: 'center', padding: 24 }}>
              {t('dashboard.noWorkspaces')}
            </div>
          )}
        </Card>
      </div>

      <Card title={t('dashboard.myPermissions')}>
        <PermissionMatrix />
      </Card>
    </div>
  );
}
