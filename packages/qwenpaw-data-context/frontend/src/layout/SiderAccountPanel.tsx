import { resolveHostModelConfigUrl } from '@/auth/config';
import { useMemo, useState } from 'react';
import type { MenuProps } from 'antd';
import { Dropdown, message } from 'antd';
import { MoonOutlined, SunOutlined } from '@ant-design/icons';
import { QwenPawDataSiderAccountCard, useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import {
  Button,
  CheckOutlined,
  GlobalOutlined,
  HomeOutlined,
  LockOutlined,
  LogoutOutlined,
  SettingOutlined,
  Tooltip,
  UserOutlined,
} from '@/design';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { switchWorkspace } from '@/auth';
import {
  useActiveWorkspace,
  useAuthEnabled,
  useAuthStore,
  useCurrentUser,
  useWorkspaces,
} from '@/store/authStore';
import { useAppI18n } from '@/i18n/useAppI18n';
import BackToHostButton from './BackToHostButton';
import styles from './SiderAccountPanel.module.css';

function getPopupContainer(triggerNode: HTMLElement): HTMLElement {
  return triggerNode.closest<HTMLElement>('[data-qwenpaw-data-sider-account-card]') ?? document.body;
}

export default function SiderAccountPanel() {
  const navigate = useNavigate();
  const authEnabled = useAuthEnabled();
  const workspaces = useWorkspaces();
  const activeWorkspace = useActiveWorkspace();
  const { username, displayName } = useCurrentUser();
  const globalRole = useAuthStore((state) => state.role);
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const role = globalRole === 'platform_admin' ? globalRole : activeWorkspaceRole;
  const [switching, setSwitching] = useState(false);
  const { t: tAuth } = useTranslation('translation', { keyPrefix: 'auth' });
  const { language, changeLanguage, t } = useAppI18n();
  const { colorMode, toggleColorMode } = useQwenPawDataColorMode();

  const userCenterLanding =
    globalRole === 'platform_admin'
      ? '/user-center/tenants'
      : role === 'member'
        ? '/user-center/home'
        : '/user-center/console';

  const userMenuItems = useMemo<MenuProps['items']>(
    () => [
      {
        key: 'user-center',
        icon: <UserOutlined />,
        label: tAuth('avatar.userCenter'),
        onClick: () => navigate(userCenterLanding),
      },
      ...(globalRole === 'platform_admin'
        ? []
        : [
            {
              key: 'change-password',
              icon: <LockOutlined />,
              label: tAuth('avatar.changePassword'),
              onClick: () => navigate('/user-center/account/change-password'),
            },
          ]),
      { type: 'divider' as const },
      {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: tAuth('avatar.logout'),
        danger: true,
        onClick: async () => {
          const loginPath = globalRole === 'platform_admin' ? '/platform/login' : '/login';
          try {
            await useAuthStore.getState().logout();
          } finally {
            window.location.assign(loginPath);
          }
        },
      },
    ],
    [globalRole, navigate, tAuth, userCenterLanding],
  );

  const workspaceItems: MenuProps['items'] = [
    {
      type: 'group',
      label: tAuth('workspaceSwitcher.switch'),
      children: workspaces.map((workspace) => ({
        key: workspace.id,
        label: (
          <span className={styles.workspaceOption}>
            <span>{workspace.name}</span>
            {workspace.id === activeWorkspace.id ? <CheckOutlined /> : null}
          </span>
        ),
        disabled: switching,
      })),
    },
  ];

  const handleWorkspaceSwitch = async (workspaceId: string) => {
    if (switching || workspaceId === activeWorkspace.id) return;
    setSwitching(true);
    try {
      await switchWorkspace(workspaceId);
      await useAuthStore.getState().refresh();
      if (useAuthStore.getState().activeWorkspaceId !== workspaceId) {
        throw new Error(tAuth('workspaceSwitcher.refreshMismatch'));
      }
      window.location.reload();
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : tAuth('workspaceSwitcher.switchFailed'),
      );
      await useAuthStore
        .getState()
        .refresh()
        .catch(() => undefined);
      setSwitching(false);
    }
  };

  const nextLanguage = language === 'zh' ? 'en' : 'zh';
  const languageLabel = t(
    nextLanguage === 'zh' ? 'common.switchLanguageToChinese' : 'common.switchLanguageToEnglish',
  );
  const modelConfigUrl = resolveHostModelConfigUrl();
  const canOpenModelConfig =
    Boolean(modelConfigUrl) && (!authEnabled || capabilities.includes('model_config'));
  const colorModeLabel =
    colorMode === 'light' ? t('common.switchToDarkMode') : t('common.switchToLightMode');

  const displayUsername = authEnabled ? displayName || username : t('common.appTitle');
  const displayWorkspace = activeWorkspace.name || tAuth(`role.${role}`);

  return (
    <QwenPawDataSiderAccountCard
      username={displayUsername}
      workspaceName={displayWorkspace}
      usernameIcon={<UserOutlined />}
      workspaceIcon={<HomeOutlined />}
      usernameAriaLabel={`${tAuth('avatar.userCenter')}: ${displayUsername}`}
      workspaceAriaLabel={`${tAuth('workspaceSwitcher.switch')}: ${displayWorkspace}`}
      renderUsernameTrigger={
        authEnabled
          ? (trigger) => (
              <Dropdown
                getPopupContainer={getPopupContainer}
                overlayClassName="qwenpaw-data-sider-account-dropdown"
                menu={{ items: userMenuItems }}
                mouseEnterDelay={0.08}
                mouseLeaveDelay={0.16}
                placement="topRight"
                trigger={['hover', 'click']}
              >
                {trigger}
              </Dropdown>
            )
          : undefined
      }
      renderWorkspaceTrigger={
        authEnabled && workspaces.length
          ? (trigger) => (
              <Dropdown
                getPopupContainer={getPopupContainer}
                overlayClassName="qwenpaw-data-sider-workspace-dropdown"
                mouseEnterDelay={0.08}
                mouseLeaveDelay={0.16}
                placement="topRight"
                trigger={['hover', 'click']}
                menu={{
                  items: workspaceItems,
                  onClick: ({ key }) => void handleWorkspaceSwitch(key),
                }}
              >
                {trigger}
              </Dropdown>
            )
          : undefined
      }
      actions={[
        {
          key: 'language',
          node: (
            <Tooltip title={languageLabel} placement="top">
              <Button
                type="text"
                icon={<GlobalOutlined />}
                aria-label={languageLabel}
                onClick={() => changeLanguage(nextLanguage)}
              />
            </Tooltip>
          ),
        },
        {
          key: 'color-mode',
          node: (
            <Tooltip title={colorModeLabel} placement="top">
              <Button
                type="text"
                icon={colorMode === 'light' ? <MoonOutlined /> : <SunOutlined />}
                aria-label={colorModeLabel}
                onClick={toggleColorMode}
              />
            </Tooltip>
          ),
        },
        {
          key: 'settings',
          node: (
            <Tooltip title={t('menu.modelConfig')} placement="top">
              <Button
                type="text"
                icon={<SettingOutlined />}
                aria-label={t('menu.modelConfig')}
                disabled={!canOpenModelConfig}
                onClick={() => {
                  if (modelConfigUrl) window.location.assign(modelConfigUrl);
                }}
              />
            </Tooltip>
          ),
        },
        {
          key: 'agent-center',
          node: <BackToHostButton />,
        },
      ]}
    />
  );
}
