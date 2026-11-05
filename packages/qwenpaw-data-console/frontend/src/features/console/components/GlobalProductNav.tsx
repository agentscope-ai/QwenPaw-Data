import { getRuntimeConfig } from '@/services/runtimeConfig';
import { capabilities } from '@/services/capabilities';
import {
  ApiOutlined,
  AppstoreOutlined,
  CalendarOutlined,
  CloseOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { Button, Input, Tooltip } from '@agentscope-ai/design';
import { useState } from 'react';
import type { ReactNode } from 'react';
import type { ConsoleSession } from '../api/types';
import { selectSidebarSessions } from '../model/selectHistorySessions';
import { getContextFrontendUrl } from '@/services/config';
import styles from '../index.module.less';
import OverflowTooltip from './OverflowTooltip';
import SessionActions from './SessionActions';

interface GlobalProductNavProps {
  mobileOpen?: boolean;
  sessions: ConsoleSession[];
  currentSession: ConsoleSession | null;
  allSessionsActive?: boolean;
  skillsHubActive?: boolean;
  mcpHubActive?: boolean;
  cronJobsActive?: boolean;
  onCreateSession: () => void;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, title: string) => Promise<unknown>;
  onDeleteSession: (sessionId: string) => Promise<unknown>;
  onViewAllSessions: () => void;
  onOpenSkillsHub: () => void;
  onOpenMcpHub: () => void;
  onOpenCronJobs: () => void;
  onClose?: () => void;
}

export default function GlobalProductNav({
  mobileOpen = false,
  sessions,
  currentSession,
  allSessionsActive = false,
  skillsHubActive = false,
  mcpHubActive = false,
  cronJobsActive = false,
  onCreateSession,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onViewAllSessions,
  onOpenSkillsHub,
  onOpenMcpHub,
  onOpenCronJobs,
  onClose,
}: GlobalProductNavProps) {
  const [searchText, setSearchText] = useState('');
  const normalizedSearch = searchText.trim().toLocaleLowerCase();
  const currentSessionId = currentSession?.id ?? null;
  const visibleSessions = selectSidebarSessions(sessions, searchText);

  const soonItem = (
    icon: ReactNode,
    label: string,
  ) => (
    <Tooltip title={`${label} 即将开放`} placement="right">
      <span className={styles.navDisabledWrap}>
        <button
          className={`${styles.navItem} ${styles.navItemDisabled}`}
          type="button"
          aria-disabled="true"
          onClick={(event) => event.preventDefault()}
        >
          {icon}
          <span>{label}</span>
          <span className={styles.navSoon}>Soon</span>
        </button>
      </span>
    </Tooltip>
  );

  return (
    <aside className={`${styles.globalNav} ${mobileOpen ? styles.globalNavMobileOpen : ''}`}>
      <div className={styles.navBrand}>
        <button
          className={`${styles.brandWordmark} ${styles.brandHomeButton}`}
          type="button"
          aria-label="返回新建会话"
          title="返回新建会话"
          onClick={() => {
            onCreateSession();
            onClose?.();
          }}
        >
          <img src={getRuntimeConfig().logo_url} alt={getRuntimeConfig().title} />
          <img
            className={styles.brandWordmarkContrast}
            src={getRuntimeConfig().logo_url}
            alt=""
            aria-hidden="true"
          />
        </button>
        <Button
          className={styles.mobileNavClose}
          type="text"
          icon={<CloseOutlined />}
          aria-label="关闭导航"
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
        新建数据会话
      </Button>

      <nav className={styles.navBody} aria-label="全局导航">
        <div className={styles.navSectionLabel}>数据会话</div>
        <div className={styles.navSearchWrap}>
          <Input
            className={styles.navSearch}
            prefix={<SearchOutlined />}
            placeholder="搜索会话"
            aria-label="搜索会话"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
        </div>
        <div className={styles.recentSessions}>
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
                <OverflowTooltip title={session.title || '未命名会话'}>
                  <span>{session.title || '未命名会话'}</span>
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
          {!visibleSessions.length ? (
            <span className={styles.navSessionEmpty}>
              {normalizedSearch ? '没有匹配的会话' : '暂无历史会话'}
            </span>
          ) : null}
        </div>

        <button
          className={`${styles.navItem} ${
            allSessionsActive ? styles.navItemActive : ''
          }`}
          type="button"
          aria-current={allSessionsActive ? 'page' : undefined}
          onClick={() => {
            onViewAllSessions();
            onClose?.();
          }}
        >
          <FolderOpenOutlined />
          <span>全部会话</span>
        </button>

        <div className={styles.navDivider} />
        <div className={styles.navSectionLabel}>工作空间</div>
        <a
          className={styles.navItem}
          hidden={!capabilities.contextConsole}
          href={getContextFrontendUrl()}
          target="_blank"
          rel="noopener noreferrer"
        >
          <DatabaseOutlined />
          <span>数据管理</span>
        </a>
        <button
          hidden={!capabilities.skillsHub}
          className={`${styles.navItem} ${skillsHubActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={skillsHubActive ? 'page' : undefined}
          onClick={() => {
            onOpenSkillsHub();
            onClose?.();
          }}
        >
          <ThunderboltOutlined />
          <span>Skills Hub</span>
        </button>
        <button
          hidden={!capabilities.mcpHub}
          className={`${styles.navItem} ${mcpHubActive ? styles.navItemActive : ''}`}
          type="button"
          aria-current={mcpHubActive ? 'page' : undefined}
          onClick={() => {
            onOpenMcpHub();
            onClose?.();
          }}
        >
          <ApiOutlined />
          <span>MCP Hub</span>
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
          <span>Cron Jobs</span>
        </button>
        {soonItem(<ExperimentOutlined />, '评测')}
        {soonItem(<AppstoreOutlined />, '成果库')}
      </nav>

      <div className={styles.navFooter}>
        <button
          className={`${styles.workspaceSwitcher} ${styles.workspaceSwitcherDisabled}`}
          type="button"
          aria-label="当前 Workspace（不可切换）"
          disabled
        >
          <span className={styles.workspaceSwitcherIcon}><AppstoreOutlined /></span>
          <span className={styles.workspaceSwitcherCopy}>
            <span>当前 Workspace</span>
            <OverflowTooltip title={getRuntimeConfig().workspace_name}>
              <strong>{getRuntimeConfig().workspace_name}</strong>
            </OverflowTooltip>
          </span>
        </button>
      </div>
    </aside>
  );
}
