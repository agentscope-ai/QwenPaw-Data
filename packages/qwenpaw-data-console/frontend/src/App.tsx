import { ConfigProvider } from '@agentscope-ai/design';
import { useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { lazy, Suspense, useLayoutEffect, useMemo, type ReactNode } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppI18nProvider, useAppI18n } from './i18n';
import ConsoleLayout from './layouts/ConsoleLayout';
import { createHostThemeProviderProps } from './theme';
import styles from './App.module.less';

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
  useLayoutEffect(() => {
    document.getElementById('initial-loading')?.remove();
  }, []);

  return <>{children}</>;
}

function LocalizedApp() {
  const { language, t } = useAppI18n();
  const { colorMode } = useQwenPawDataColorMode();
  const themeProviderProps = useMemo(() => createHostThemeProviderProps(colorMode), [colorMode]);

  return (
    <ConfigProvider {...themeProviderProps} locale={language === 'zh' ? zhCN : enUS}>
      <BrowserRouter>
        <Suspense fallback={<AppLoadingFallback label={t('common.loading')} />}>
          <Routes>
            <Route path="/console" element={<ConsoleLayout />}>
              <Route
                index
                element={
                  <InitialLoadingCleanup>
                    <ConsolePage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="sessions"
                element={
                  <InitialLoadingCleanup>
                    <SessionsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="skills"
                element={
                  <InitialLoadingCleanup>
                    <SkillsHubPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="cron-jobs"
                element={
                  <InitialLoadingCleanup>
                    <CronJobsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="settlement"
                element={
                  <InitialLoadingCleanup>
                    <SettlementCenterPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="settings/agent"
                element={
                  <InitialLoadingCleanup>
                    <AgentSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="settings/channels"
                element={
                  <InitialLoadingCleanup>
                    <ChannelSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="settings/mcp"
                element={
                  <InitialLoadingCleanup>
                    <McpSettingsPage />
                  </InitialLoadingCleanup>
                }
              />
              <Route
                path="settings/usage"
                element={
                  <InitialLoadingCleanup>
                    <UsagePage />
                  </InitialLoadingCleanup>
                }
              />
            </Route>
            <Route path="*" element={<RedirectToConsole />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
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
