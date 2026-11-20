import { getAgentSettingsReturnPath } from '@/shared/navigation/sessionNavigation';
import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { LeftOutlined, RightOutlined } from '@ant-design/icons';
import { Outlet, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import GlobalProductNav from '@/features/console/components/GlobalProductNav/index';
import {
  buildAllConsoleSessionsPath,
  buildConsoleSessionPath,
  buildAgentConfigurationPath,
  buildChannelConfigurationPath,
  buildMcpConfigurationPath,
  buildCronJobsPath,
  buildSettlementCenterPath,
  buildNewConsoleSessionPath,
  buildSkillsHubPath,
  buildUsagePath,
} from '@/shared/navigation/sessionNavigation';
import { useConsoleChat } from '@/features/console/model/chat/useConsoleChat';
import {
  SESSION_CATALOG_POLL_INTERVAL_MS,
  shouldRefreshSessionCatalog,
} from '@/features/console/model/session/sessionCatalogPolling';
import sharedStyles from '@/features/console/index.module.less';
import { mergeStyleModules } from '@/features/console/styleModules';
import { useAppI18n } from '@/i18n';
import type { ConsoleLayoutContext } from './context';
import { useSettlementCenter } from '@/features/console/model/settlement/useSettlementCenter';
import ownerStyles from './index.module.less';
import { RuntimeProvider } from '@/features/console/ui-runtime/RuntimeProvider';

const styles = mergeStyleModules(ownerStyles, sharedStyles);
const NARROW_LAYOUT_QUERY = '(max-width: 760px)';

export default function ConsoleLayout() {
  const { t } = useAppI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const retainedSession = useRef<string | null>(null);
  const retainedDatasource = useRef<string | null>(null);
  const onConsoleRoute = location.pathname.startsWith('/console');
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(false);
  const allSessionsPath = buildAllConsoleSessionsPath();
  const showingAllSessions = location.pathname === allSessionsPath;
  const skillsHubPath = buildSkillsHubPath();
  const showingSkillsHub = location.pathname === skillsHubPath;
  const cronJobsPath = buildCronJobsPath();
  const showingCronJobs = location.pathname === cronJobsPath;
  const settlementCenterPath = buildSettlementCenterPath();
  const showingSettlementCenter = location.pathname.replace(/\/+$/, '') === settlementCenterPath;
  const settlementController = useSettlementCenter(showingSettlementCenter);
  const agentConfigurationPath = buildAgentConfigurationPath();
  const showingAgentConfiguration = location.pathname === agentConfigurationPath;
  const channelConfigurationPath = buildChannelConfigurationPath();
  const showingChannelConfiguration = location.pathname === channelConfigurationPath;
  const mcpConfigurationPath = buildMcpConfigurationPath();
  const showingMcpConfiguration = location.pathname === mcpConfigurationPath;
  const showingUsage = location.pathname.replace(/\/+$/, '') === buildUsagePath();
  const settingsReturnPath = showingAgentConfiguration
    ? getAgentSettingsReturnPath(location.search)
    : null;
  const requestedSessionId = settingsReturnPath
    ? new URLSearchParams(settingsReturnPath.split('?')[1] ?? '').get('session_id')
    : showingAllSessions ||
        showingSkillsHub ||
        showingCronJobs ||
        showingSettlementCenter ||
        showingAgentConfiguration ||
        showingChannelConfiguration ||
        showingMcpConfiguration ||
        showingUsage
      ? retainedSession.current
      : onConsoleRoute
        ? searchParams.get('session_id')
        : retainedSession.current;
  useLayoutEffect(() => {
    if (location.pathname === '/console') retainedSession.current = requestedSessionId;
  }, [location.pathname, requestedSessionId]);
  if (location.pathname === '/console')
    retainedDatasource.current = searchParams.get('datasource_id');
  const requestedDatasourceId = retainedDatasource.current;
  const handleSessionResolved = useCallback(
    (session: { id: string }) => {
      retainedSession.current = session.id;
      if (location.pathname === '/console')
        navigate(buildConsoleSessionPath(session.id), { replace: true });
    },
    [navigate, location.pathname],
  );
  const consoleController = useConsoleChat(
    requestedSessionId,
    handleSessionResolved,
    requestedDatasourceId,
  );
  const reloadSessions = consoleController.reloadSessions;
  useEffect(() => {
    const mediaQuery = window.matchMedia(NARROW_LAYOUT_QUERY);
    const syncResponsiveNavigation = () => {
      setMobileNavOpen(false);
      // Mobile visibility is controlled by the drawer's own off-canvas state.
      // Reusing the desktop collapsed state also translates the drawer away,
      // which leaves the overlay visible with no navigation panel.
      setNavCollapsed(false);
    };
    const handleViewportChange = () => {
      syncResponsiveNavigation();
    };

    syncResponsiveNavigation();
    mediaQuery.addEventListener('change', handleViewportChange);
    return () => mediaQuery.removeEventListener('change', handleViewportChange);
  }, []);

  useEffect(() => {
    const refreshIfVisible = () => {
      if (shouldRefreshSessionCatalog(document.visibilityState)) {
        reloadSessions();
      }
    };
    const intervalId = window.setInterval(refreshIfVisible, SESSION_CATALOG_POLL_INTERVAL_MS);
    document.addEventListener('visibilitychange', refreshIfVisible);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', refreshIfVisible);
    };
  }, [reloadSessions]);
  const openMobileNav = useCallback(() => setMobileNavOpen(true), []);
  const layoutContext = useMemo<ConsoleLayoutContext>(
    () => ({
      consoleController,
      settlementController,
      openMobileNav,
    }),
    [consoleController, settlementController, openMobileNav],
  );

  const hostShell = (
    <div className={`${styles.page} ${navCollapsed ? styles.pageNavCollapsed : ''}`}>
      {mobileNavOpen ? (
        <button
          className={styles.mobileNavOverlay}
          type="button"
          aria-label={t('nav.close')}
          onClick={() => setMobileNavOpen(false)}
        />
      ) : null}
      <GlobalProductNav
        mobileOpen={mobileNavOpen}
        sessions={consoleController.sessions}
        sessionsLoadingMore={consoleController.sessionsLoadingMore}
        sessionsHasMore={consoleController.sessionsHasMore}
        currentSession={consoleController.session}
        allSessionsActive={showingAllSessions}
        skillsHubActive={showingSkillsHub}
        cronJobsActive={showingCronJobs}
        settlementCenterActive={showingSettlementCenter}
        settlementUnreadCount={settlementController.unreadCount}
        onCreateSession={() => {
          retainedSession.current = null;
          consoleController.startNewSession();
          navigate(buildNewConsoleSessionPath(), { replace: true });
        }}
        onSelectSession={(sessionId) => {
          if (location.pathname !== '/console' || searchParams.get('session_id') !== sessionId) {
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
        onSearchSessions={consoleController.searchSessions}
        onLoadMoreSessions={consoleController.loadMoreSessions}
        onViewAllSessions={() => navigate(allSessionsPath)}
        onOpenSkillsHub={() => navigate(skillsHubPath)}
        onOpenCronJobs={() => navigate(cronJobsPath)}
        onOpenSettlementCenter={() => navigate(settlementCenterPath)}
        onClose={() => setMobileNavOpen(false)}
      />
      <button
        className={styles.navCollapseButton}
        type="button"
        aria-expanded={!navCollapsed}
        aria-label={navCollapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
        title={navCollapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
        onClick={() => setNavCollapsed((collapsed) => !collapsed)}
      >
        {navCollapsed ? <RightOutlined /> : <LeftOutlined />}
      </button>
      <div className={styles.routeContent}>
        <Suspense
          fallback={
            <div role="status" style={{ padding: 24 }}>
              {t('common.loading')}
            </div>
          }
        >
          <Outlet context={layoutContext} />
        </Suspense>
      </div>
    </div>
  );

  return (
    <>
      <RuntimeProvider sessionId={consoleController.session?.id ?? requestedSessionId} />
      {!onConsoleRoute ? (
        <Suspense fallback={<div role="status">{t('common.loading')}</div>}>
          <Outlet context={layoutContext} />
        </Suspense>
      ) : (
        hostShell
      )}
    </>
  );
}
