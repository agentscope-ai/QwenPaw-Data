import { ConfigProvider } from '@agentscope-ai/design';
import { useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { lazy, Suspense, useLayoutEffect, useMemo, type ReactNode } from 'react';
import {
  createBrowserRouter,
  createRoutesFromElements,
  RouterProvider,
  Navigate,
  Route,
  useLocation,
} from 'react-router-dom';
import { createContextRoutes, configureEmbedding, authPlane } from '@qwenpaw-data/cm';
import { EmbeddedContext, UnifiedEntry } from './features/console/ui-runtime/UnifiedEntry';
import '../../../qwenpaw-data-context/frontend/dist-embedded/context.css';
import { AppI18nProvider, useAppI18n } from './i18n';
import { createHostThemeProviderProps } from './theme';
import styles from './App.module.less';
import { uiRegistry } from './features/console/ui-runtime/registry';

const ConsoleLayout = lazy(() => import('./layouts/ConsoleLayout'));
const AgentSettingsPage = lazy(() => import('./pages/AgentSettings'));
const ChannelSettingsPage = lazy(() => import('./pages/ChannelSettings'));
const McpSettingsPage = lazy(() => import('./pages/McpSettings'));
const UsagePage = lazy(() => import('./pages/Usage'));
const ConsolePage = lazy(() => import('./pages/Console'));
const CronJobsPage = lazy(() => import('./pages/CronJobs'));
const SettlementCenterPage = lazy(() => import('./pages/SettlementCenter'));
const SessionsPage = lazy(() => import('./pages/Sessions'));
const SkillsHubPage = lazy(() => import('./pages/SkillsHub'));

function RedirectToConsole() {
  const location = useLocation();
  return <Navigate replace to={`/console${location.search}${location.hash}`} />;
}

function AppLoading({ label }: { label: string }) {
  const { colorMode } = useQwenPawDataColorMode();
  const darkAssetSuffix = colorMode === 'dark' ? '-dark' : '';
  const prefersReducedMotion =
    globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

  return (
    <div className={styles.appLoading} role="status" aria-live="polite">
      <div className={styles.appLoadingVisual} aria-hidden="true">
        {prefersReducedMotion ? (
          <img
            className={styles.appLoadingMascot}
            src={`/snow-leopard-loading${darkAssetSuffix}.png`}
            alt=""
          />
        ) : (
          <video
            className={styles.appLoadingMascot}
            src={`/snow-leopard-loading${darkAssetSuffix}.mp4`}
            autoPlay
            loop
            muted
            playsInline
            preload="auto"
            disablePictureInPicture
          />
        )}
      </div>
      <span>{label}</span>
    </div>
  );
}

function AppLoadingFallback({ label }: { label: string }) {
  if (document.getElementById('initial-loading')) return null;
  return <AppLoading label={label} />;
}

function InitialLoadingCleanup({ children }: { children: ReactNode }) {
  const location = useLocation();
  useLayoutEffect(() => {
    document.getElementById('initial-loading')?.remove();
    uiRegistry.ready(location.pathname);
  }, [location.pathname]);

  return <>{children}</>;
}

configureEmbedding();
const contextRoutes = createContextRoutes();
const contextMain = contextRoutes.find((route) => route.path === '/' && route.children)!;
const tenantChildren = contextMain.children!.filter(
  (route) => route.path !== '/' && authPlane(route.path ?? '') === 'tenant',
);
const platformChildren = contextMain.children!.filter(
  (route) => authPlane(route.path ?? '') === 'platform',
);
const wrapContext = (element: ReactNode) => <EmbeddedContext>{element}</EmbeddedContext>;
const router = createBrowserRouter([
  {
    element: <UnifiedEntry />,
    children: [
      {
        element: <ConsoleLayout />,
        children: [
          ...createRoutesFromElements(
            <Route id="host-console" path="/console">
              <Route
                id="host-console-index"
                index
                element={
                  <InitialLoadingCleanup>
                    <ConsolePage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-sessions"
                path="sessions"
                element={
                  <InitialLoadingCleanup>
                    <SessionsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-skills"
                path="skills"
                element={
                  <InitialLoadingCleanup>
                    <SkillsHubPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-cron-jobs"
                path="cron-jobs"
                element={
                  <InitialLoadingCleanup>
                    <CronJobsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-settlement"
                path="settlement"
                element={
                  <InitialLoadingCleanup>
                    <SettlementCenterPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-settings-agent"
                path="settings/agent"
                element={
                  <InitialLoadingCleanup>
                    <AgentSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-settings-channels"
                path="settings/channels"
                element={
                  <InitialLoadingCleanup>
                    <ChannelSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-settings-mcp"
                path="settings/mcp"
                element={
                  <InitialLoadingCleanup>
                    <McpSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                id="host-console-settings-usage"
                path="settings/usage"
                element={
                  <InitialLoadingCleanup>
                    <UsagePage />
                  </InitialLoadingCleanup>
                }
              />
            </Route>,
          ),
          {
            ...contextMain,
            index: false,
            element: wrapContext(contextMain.element),
            children: tenantChildren,
          },
          { index: true, element: <RedirectToConsole /> },
        ],
      },
      ...contextRoutes
        .filter((route) => route !== contextMain)
        .map((route) => ({
          ...route,
          element: route.element ? wrapContext(route.element) : undefined,
        })),
      {
        ...contextMain,
        index: false,
        element: wrapContext(contextMain.element),
        children: platformChildren,
      },
    ],
  },
]);

function LocalizedApp() {
  const { language, t } = useAppI18n();
  const { colorMode } = useQwenPawDataColorMode();
  const themeProviderProps = useMemo(() => createHostThemeProviderProps(colorMode), [colorMode]);

  return (
    <ConfigProvider {...themeProviderProps} locale={language === 'zh' ? zhCN : enUS}>
      <Suspense fallback={<AppLoadingFallback label={t('common.loading')} />}>
        <RouterProvider router={router} />
      </Suspense>
    </ConfigProvider>
  );
}

export default function App() {
  return (
    <AppI18nProvider>
      <LocalizedApp />
    </AppI18nProvider>
  );
}
