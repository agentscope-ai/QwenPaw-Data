import {
  CalendarOutlined,
  CheckOutlined,
  CloseOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  GlobalOutlined,
  HomeOutlined,
  InboxOutlined,
  LockOutlined,
  LoadingOutlined,
  LogoutOutlined,
  MoonOutlined,
  PlusOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  SunOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Input, Tooltip } from '@agentscope-ai/design';
import { QwenPawDataSiderAccountCard, useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import type { MenuProps } from 'antd';
import { message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import {
  getHostCurrentUser,
  logoutHostSession,
  redirectToLogin,
  switchHostWorkspace,
} from '@/services/auth';
import { useNavigate } from 'react-router-dom';
import type { ConsoleSession } from '../../api/types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import ConsoleSettingsMenu from '../ConsoleSettingsMenu/index';
import { useAppI18n } from '@/i18n';
import OverflowTooltip from '../OverflowTooltip';
import SessionActions from '../SessionActions';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

function getAccountCardPopupContainer(triggerNode: HTMLElement): HTMLElement {
  return triggerNode.closest<HTMLElement>('[data-qwenpaw-data-sider-account-card]') ?? document.body;
}

interface GlobalProductNavProps {
  mobileOpen?: boolean;
  sessions: ConsoleSession[];
  sessionsLoadingMore: boolean;
  sessionsHasMore: boolean;
  currentSession: ConsoleSession | null;
  allSessionsActive?: boolean;
  skillsHubActive?: boolean;
  cronJobsActive?: boolean;
  settlementCenterActive?: boolean;
  settlementUnreadCount?: number;
  onCreateSession: () => void;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, title: string) => Promise<unknown>;
  onDeleteSession: (sessionId: string) => Promise<unknown>;
  onSearchSessions: (searchText: string) => void;
  onLoadMoreSessions: () => void;
  onViewAllSessions: () => void;
  onOpenSkillsHub: () => void;
  onOpenCronJobs: () => void;
  onOpenSettlementCenter: () => void;
  onClose?: () => void;
}

export default function GlobalProductNav({
  mobileOpen = false,
  sessions,
  sessionsLoadingMore,
  sessionsHasMore,
  currentSession,
  allSessionsActive = false,
  skillsHubActive = false,
  cronJobsActive = false,
  settlementCenterActive = false,
  settlementUnreadCount = 0,
  onCreateSession,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onSearchSessions,
  onLoadMoreSessions,
  onViewAllSessions,
  onOpenSkillsHub,
  onOpenCronJobs,
  onOpenSettlementCenter,
  onClose,
}: GlobalProductNavProps) {
  const navigate = useNavigate();
  const [searchText, setSearchText] = useState('');
  const lastSubmittedSearch = useRef('');
  const [workspaceSwitching, setWorkspaceSwitching] = useState(false);
  const { language, t, toggleLanguage } = useAppI18n();
  const { colorMode, toggleColorMode } = useQwenPawDataColorMode();
  const currentUser = getHostCurrentUser();
  const workspaces = currentUser?.workspaces ?? [];
  const activeWorkspaceId = currentUser?.active_workspace_id ?? '';
  const username = currentUser?.display_name || currentUser?.username || t('nav.currentUser');
  const activeWorkspaceName =
    currentUser?.active_workspace_name ||
    workspaces.find((workspace) => workspace.id === activeWorkspaceId)?.name ||
    t('nav.enterpriseDataSpace');
  const normalizedSearch = searchText.trim().toLocaleLowerCase();
  const currentSessionId = settlementCenterActive ? null : (currentSession?.id ?? null);
  const visibleSessions = sessions;
  const languageLabel =
    language === 'zh' ? t('common.switchToEnglish') : t('common.switchToChinese');
  const colorModeLabel =
    colorMode === 'light' ? t('common.switchToDarkMode') : t('common.switchToLightMode');

  useEffect(() => {
    const normalized = searchText.trim();
    if (normalized === lastSubmittedSearch.current) return undefined;
    const timer = window.setTimeout(() => {
      lastSubmittedSearch.current = normalized;
      onSearchSessions(normalized);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchText, onSearchSessions]);

  const openContextPath = (path: string) => {
    navigate(path);
    onClose?.();
  };

  const handleLogout = async () => {
    try {
      await logoutHostSession();
    } finally {
      redirectToLogin();
    }
  };

  const userMenuItems: MenuProps['items'] = [
    {
      key: 'user-center',
      icon: <UserOutlined />,
      label: t('nav.userCenter'),
      onClick: () => openContextPath('/user-center/home'),
    },
    {
      key: 'change-password',
      icon: <LockOutlined />,
      label: t('nav.changePassword'),
      onClick: () => openContextPath('/user-center/account/change-password'),
    },
    { type: 'divider' },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: t('nav.logout'),
      danger: true,
      onClick: () => void handleLogout(),
    },
  ];

  const handleWorkspaceSwitch = async (workspaceId: string) => {
    if (workspaceSwitching || workspaceId === activeWorkspaceId) return;
    setWorkspaceSwitching(true);
    try {
      await switchHostWorkspace(workspaceId);
      window.location.reload();
    } catch (error) {
      message.error(
        t('nav.switchWorkspaceFailed', {
          message: error instanceof Error ? error.message : String(error),
        }),
      );
      setWorkspaceSwitching(false);
    }
  };

  const workspaceMenuItems: MenuProps['items'] = [
    {
      type: 'group',
      label: t('nav.switchWorkspace'),
      children: workspaces.map((workspace) => {
        const active = workspace.id === activeWorkspaceId;
        return {
          key: workspace.id,
          className: active ? styles.workspaceMenuItemActive : undefined,
          label: (
            <span
              className={`${styles.workspaceMenuOption} ${
                active ? styles.workspaceMenuOptionActive : ''
              }`}
            >
              <span>{workspace.name}</span>
              {active ? <CheckOutlined className={styles.workspaceMenuCheck} /> : null}
            </span>
          ),
          disabled: workspaceSwitching,
        };
      }),
    },
  ];

  return (
    <aside className={`${styles.globalNav} ${mobileOpen ? styles.globalNavMobileOpen : ''}`}>
      <div className={styles.navBrand}>
        <button
          className={`${styles.brandWordmark} ${styles.brandHomeButton}`}
          type="button"
          aria-label={t('nav.backToNewSession')}
          title={t('nav.backToNewSession')}
          onClick={() => {
            onCreateSession();
            onClose?.();
          }}
        >
          <img src={`${import.meta.env.BASE_URL}qwenpaw-data-brand.png`} alt="QwenPaw Data" />
          <img
            className={styles.brandWordmarkContrast}
            src={`${import.meta.env.BASE_URL}qwenpaw-data-brand.png`}
            alt=""
            aria-hidden="true"
          />
        </button>
        <Button
          className={styles.mobileNavClose}
          type="text"
          icon={<CloseOutlined />}
          aria-label={t('nav.close')}
          onClick={onClose}
        />
      </div>

      <Button
        className={styles.newSessionButton}
        type="primary"
        icon={<PlusOutlined />}
        onClick={() => {
          onCreateSession();
          onClose?.();
        }}
      >
        {t('nav.newSession')}
      </Button>

      <nav className={styles.navBody} aria-label={t('nav.global')}>
        <div className={styles.navSectionLabel}>{t('nav.dataSessions')}</div>
        <div className={styles.navSearchWrap}>
          <Input
            className={styles.navSearch}
            prefix={<SearchOutlined />}
            placeholder={t('nav.searchSession')}
            aria-label={t('nav.searchSession')}
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
        </div>
        <div
          className={styles.recentSessions}
          onScroll={(event) => {
            const list = event.currentTarget;
            if (sessionsHasMore && list.scrollHeight - list.scrollTop - list.clientHeight < 48) {
              onLoadMoreSessions();
            }
          }}
        >
          {visibleSessions.map((session) => (
            <div
              className={`${styles.recentSession} ${
                session.id === currentSessionId ? styles.recentSessionActive : ''
              }`}
              key={session.id}
            >
              <button
                type="button"
                className={styles.recentSessionMain}
                aria-current={session.id === currentSessionId ? 'page' : undefined}
                onClick={() => {
                  onSelectSession(session.id);
                  onClose?.();
                }}
              >
                <OverflowTooltip title={session.title || t('common.unnamedSession')}>
                  <span>{session.title || t('common.unnamedSession')}</span>
                </OverflowTooltip>
              </button>
              <SessionActions
                className={styles.recentSessionActions}
                session={session}
                tone="light"
                onRename={onRenameSession}
                onDelete={onDeleteSession}
              />
            </div>
          ))}
          {sessionsLoadingMore ? (
            <span
              className={styles.navSessionLoading}
              role="status"
              aria-label={t('common.loading')}
            >
              <LoadingOutlined spin />
            </span>
          ) : null}
          {!visibleSessions.length && !sessionsLoadingMore ? (
            <span className={styles.navSessionEmpty}>
              {normalizedSearch ? t('nav.noMatchedSession') : t('nav.noHistorySession')}
            </span>
          ) : null}
        </div>

        <button
          className={`${styles.navItem} ${allSessionsActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={allSessionsActive ? 'page' : undefined}
          onClick={() => {
            onViewAllSessions();
            onClose?.();
          }}
        >
          <FolderOpenOutlined />
          <span>{t('nav.allSessions')}</span>
        </button>

        <div className={styles.navDivider} />
        <div className={styles.navSectionLabel}>{t('nav.workspace')}</div>
        <button
          className={`${styles.navItem} ${skillsHubActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={skillsHubActive ? 'page' : undefined}
          onClick={() => {
            onOpenSkillsHub();
            onClose?.();
          }}
        >
          <ThunderboltOutlined />
          <span>{t('nav.skillsHub')}</span>
        </button>

        <div className={styles.navDivider} />
        <div className={styles.navSectionLabel}>{t('nav.resultCenter')}</div>
        <button
          className={`${styles.navItem} ${settlementCenterActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={settlementCenterActive ? 'page' : undefined}
          aria-label={
            settlementUnreadCount > 0
              ? t('nav.settlementCenterUnread', { count: settlementUnreadCount })
              : t('nav.settlementCenter')
          }
          onClick={() => {
            onOpenSettlementCenter();
            onClose?.();
          }}
        >
          <InboxOutlined />
          <span>{t('nav.settlementCenter')}</span>
          {settlementUnreadCount > 0 ? (
            <span className={styles.settlementUnreadDot} aria-hidden="true" />
          ) : null}
        </button>
        <button
          className={`${styles.navItem} ${cronJobsActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={cronJobsActive ? 'page' : undefined}
          onClick={() => {
            onOpenCronJobs();
            onClose?.();
          }}
        >
          <CalendarOutlined />
          <span>{t('nav.cronJobs')}</span>
        </button>
      </nav>

      <div className={styles.navFooter}>
        <QwenPawDataSiderAccountCard
          username={username}
          workspaceName={activeWorkspaceName}
          usernameIcon={<UserOutlined />}
          workspaceIcon={<HomeOutlined />}
          usernameAriaLabel={`${t('nav.currentUser')}: ${username}`}
          workspaceAriaLabel={`${t('nav.currentWorkspace')}: ${activeWorkspaceName}`}
          renderUsernameTrigger={(trigger) => (
            <Dropdown
              getPopupContainer={getAccountCardPopupContainer}
              overlayClassName="qwenpaw-data-sider-account-dropdown"
              menu={{ items: userMenuItems }}
              mouseEnterDelay={0.08}
              mouseLeaveDelay={0.16}
              placement="topRight"
              trigger={['hover', 'click']}
            >
              {trigger}
            </Dropdown>
          )}
          renderWorkspaceTrigger={
            workspaces.length
              ? (trigger) => (
                  <Dropdown
                    getPopupContainer={getAccountCardPopupContainer}
                    overlayClassName={`${styles.workspaceMenuDropdown} qwenpaw-data-sider-workspace-dropdown`}
                    mouseEnterDelay={0.08}
                    mouseLeaveDelay={0.16}
                    placement="topRight"
                    trigger={['hover', 'click']}
                    menu={{
                      items: workspaceMenuItems,
                      onClick: ({ key }: { key: string }) => void handleWorkspaceSwitch(key),
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
                    onClick={toggleLanguage}
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
              node: <ConsoleSettingsMenu onAction={onClose} />,
            },
            {
              key: 'semantic-center',
              node: (
                <Tooltip title={t('settings.dataBridge')} placement="top">
                  <Button
                    type="text"
                    icon={<DatabaseOutlined />}
                    aria-label={t('settings.dataBridge')}
                    href="/data-source"
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={() => onClose?.()}
                  />
                </Tooltip>
              ),
            },
          ]}
        />
      </div>
    </aside>
  );
}
