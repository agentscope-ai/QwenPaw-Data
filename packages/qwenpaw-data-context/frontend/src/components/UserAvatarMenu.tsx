import React from 'react';
import { Dropdown, Tag } from 'antd';
import type { MenuProps } from 'antd';
import {
  UserOutlined,
  LogoutOutlined,
  SettingOutlined,
  LockOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { message } from 'antd';
import {
  currentPageDatasourceId,
  getHostFrontendUrl,
  isAgentCenterSameApp,
  resolveAgentCenterUrl,
} from '@/auth';
import { setActiveDataSource } from '@/services/dataSource';
import { useAuthStore, useCurrentUser, useAuthEnabled } from '@/store/authStore';

const ROLE_COLORS: Record<string, string> = {
  platform_admin: '#722ed1',
  tenant_admin: '#f5222d',
  ws_admin: '#fa8c16',
  member: '#1890ff',
};

const UserAvatarMenu: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const authEnabled = useAuthEnabled();
  const { username, displayName } = useCurrentUser();
  const globalRole = useAuthStore((state) => state.role);
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const role = globalRole === 'platform_admin' ? globalRole : activeWorkspaceRole;
  const navigate = useNavigate();

  if (!authEnabled) return null;

  const isPlatformAdmin = role === 'platform_admin';

  const handleLogout = async () => {
    const loginPath = isPlatformAdmin ? '/platform/login' : '/login';
    try {
      await useAuthStore.getState().logout();
    } catch {
      /* ignore */
    }
    window.location.href = loginPath;
  };

  const userCenterLanding = isPlatformAdmin
    ? '/user-center/tenants'
    : role === 'member'
      ? '/user-center/home'
      : '/user-center/dashboard';

  // 未部署智能体中心时 getHostFrontendUrl() 返回空串，此时不渲染入口；
  // 否则 window.open('') 会开一个空白新标签页。
  const hostFrontendUrl = getHostFrontendUrl();

  const items: MenuProps['items'] = [
    {
      key: 'user-center',
      icon: <SettingOutlined />,
      label: t('avatar.userCenter'),
      onClick: () => navigate(userCenterLanding),
    },
    ...(isPlatformAdmin
      ? []
      : [
          {
            key: 'change-password',
            icon: <LockOutlined />,
            label: t('avatar.changePassword'),
            onClick: () => navigate('/user-center/account/change-password'),
          },
        ]),
    ...(hostFrontendUrl
      ? [
          {
            key: 'agent-center',
            icon: <RobotOutlined />,
            label: t('avatar.agentCenter'),
            onClick: () => {
              const url = resolveAgentCenterUrl();
              if (isPlatformAdmin && isAgentCenterSameApp(url)) {
                message.info(t('avatar.agentCenterNeedsTenant'));
                return;
              }
              const datasourceId = currentPageDatasourceId();
              if (datasourceId) {
                void setActiveDataSource(datasourceId).catch(() => undefined);
              }
              window.location.assign(url);
            },
          },
        ]
      : []),
    { type: 'divider' },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: t('avatar.logout'),
      danger: true,
      onClick: handleLogout,
    },
  ];

  return (
    <Dropdown menu={{ items }} placement="bottomRight" trigger={['click']}>
      <div
        style={{
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '0 8px',
        }}
      >
        <UserOutlined style={{ fontSize: 16 }} />
        <span style={{ fontSize: 13 }}>{displayName || username}</span>
        <Tag color={ROLE_COLORS[role] || '#1890ff'} style={{ marginLeft: 2, marginRight: 0 }}>
          {t(`role.${role}`)}
        </Tag>
      </div>
    </Dropdown>
  );
};

export default UserAvatarMenu;
