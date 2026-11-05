import { useCallback, useEffect, useMemo, useState } from 'react';
import { LeftOutlined, MenuOutlined } from '@ant-design/icons';
import {
  Outlet,
  useLocation,
  useNavigate,
  useSearchParams,
} from 'react-router-dom';
import GlobalProductNav from '@/features/console/components/GlobalProductNav';
import {
  buildAllConsoleSessionsPath,
  buildConsoleSessionPath,
  buildAgentConfigurationPath,
  buildChannelConfigurationPath,
  buildCronJobsPath,
  buildMcpHubPath,
  buildNewConsoleSessionPath,
  buildSkillsHubPath,
  getSessionCatalogReloadKey,
} from '@/features/console/model/sessionNavigation';
import {
  useConsoleChat,
} from '@/features/console/model/useConsoleChat';
import {
  SESSION_CATALOG_POLL_INTERVAL_MS,
  shouldRefreshSessionCatalog,
} from '@/features/console/model/sessionCatalogPolling';
import styles from '@/features/console/index.module.less';
import type { ConsoleLayoutContext } from './context';

export default function ConsoleLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(false);
  const allSessionsPath = buildAllConsoleSessionsPath();
  const showingAllSessions = location.pathname === allSessionsPath;
  const skillsHubPath = buildSkillsHubPath();
  const showingSkillsHub = location.pathname === skillsHubPath;
  const mcpHubPath = buildMcpHubPath();
  const showingMcpHub = location.pathname === mcpHubPath;
  const cronJobsPath = buildCronJobsPath();
  const showingCronJobs = location.pathname === cronJobsPath;
  const agentConfigurationPath = buildAgentConfigurationPath();
  const showingAgentConfiguration = location.pathname === agentConfigurationPath;
  const channelConfigurationPath = buildChannelConfigurationPath();
  const showingChannelConfiguration = location.pathname === channelConfigurationPath;
  const requestedSessionId = showingAllSessions
    || showingSkillsHub
    || showingMcpHub
    || showingCronJobs
    || showingAgentConfiguration
    || showingChannelConfiguration
    ? null
    : searchParams.get('session_id');
  const sessionCatalogReloadKey = getSessionCatalogReloadKey(
    location.pathname,
    location.key,
  );
  const handleSessionResolved = useCallback((session: { id: string }) => {
    navigate(buildConsoleSessionPath(session.id), { replace: true });
  }, [navigate]);
  const consoleController = useConsoleChat(
    requestedSessionId,
    handleSessionResolved,
    sessionCatalogReloadKey,
  );
  const reloadSessions = consoleController.reloadSessions;
  useEffect(() => {
    const refreshIfVisible = () => {
      if (shouldRefreshSessionCatalog(document.visibilityState)) {
        reloadSessions();
      }
    };
    const intervalId = window.setInterval(
      refreshIfVisible,
      SESSION_CATALOG_POLL_INTERVAL_MS,
    );
    document.addEventListener('visibilitychange', refreshIfVisible);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', refreshIfVisible);
    };
  }, [reloadSessions]);
  const openMobileNav = useCallback(() => setMobileNavOpen(true), []);
  const layoutContext = useMemo<ConsoleLayoutContext>(() => ({
    consoleController,
    openMobileNav,
  }), [consoleController, openMobileNav]);

  return (
    <div className={`${styles.page} ${navCollapsed ? styles.pageNavCollapsed : ''}`}>
      {mobileNavOpen ? (
        <button
          className={styles.mobileNavOverlay}
          type="button"
          aria-label="关闭导航"
          onClick={() => setMobileNavOpen(false)}
        />
      ) : null}
      <GlobalProductNav
        mobileOpen={mobileNavOpen}
        sessions={consoleController.sessions}
        currentSession={consoleController.session}
        allSessionsActive={showingAllSessions}
        skillsHubActive={showingSkillsHub}
        mcpHubActive={showingMcpHub}
        cronJobsActive={showingCronJobs}
        onCreateSession={() => {
          consoleController.startNewSession();
          navigate(buildNewConsoleSessionPath(), { replace: true });
        }}
        onSelectSession={(sessionId) => {
          if (
            sessionId !== consoleController.session?.id
            || showingAllSessions
            || showingSkillsHub
            || showingMcpHub
            || showingCronJobs
          ) {
            navigate(buildConsoleSessionPath(sessionId));
          }
        }}
        onRenameSession={consoleController.renameSession}
        onDeleteSession={async (sessionId) => {
          const deletingCurrentSession = sessionId === consoleController.session?.id;
          await consoleController.deleteSession(sessionId);
          if (deletingCurrentSession) {
            navigate(buildNewConsoleSessionPath(), { replace: true });
          }
        }}
        onViewAllSessions={() => navigate(allSessionsPath)}
        onOpenSkillsHub={() => navigate(skillsHubPath)}
        onOpenMcpHub={() => navigate(mcpHubPath)}
        onOpenCronJobs={() => navigate(cronJobsPath)}
        onClose={() => setMobileNavOpen(false)}
      />
      <button
        className={styles.navCollapseButton}
        type="button"
        aria-expanded={!navCollapsed}
        aria-label={navCollapsed ? '展开左侧菜单' : '向左收起菜单'}
        title={navCollapsed ? '展开左侧菜单' : '向左收起菜单'}
        onClick={() => setNavCollapsed((collapsed) => !collapsed)}
      >
        {navCollapsed ? <MenuOutlined /> : <LeftOutlined />}
      </button>
      <div className={styles.routeContent}>
        <Outlet context={layoutContext} />
      </div>
    </div>
  );
}
