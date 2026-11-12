import React, { useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react';
import { useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import { ConfigProvider } from '@/design';
import { Alert, App as AntdApp, Button, Modal } from 'antd';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { RouterProvider } from 'react-router-dom';
import router from './router';
import './i18n';
import { useAppI18n } from './i18n/useAppI18n';
import { useTranslation } from 'react-i18next';
import { createContextThemeProviderProps } from './theme';
import styles from './App.module.css';
import {
  buildLocalLoginUrl,
  useAuthInitialized,
  useAuthLoading,
  useAuthStore,
} from './store/authStore';

const AppLoading: React.FC<{ label: string }> = ({ label }) => {
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
            src={`${import.meta.env.BASE_URL}snow-leopard-loading${darkAssetSuffix}.png`}
            alt=""
          />
        ) : (
          <video
            className={styles.appLoadingMascot}
            src={`${import.meta.env.BASE_URL}snow-leopard-loading${darkAssetSuffix}.mp4`}
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
};

const InitialLoadingCleanup: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  useLayoutEffect(() => {
    document.getElementById('initial-loading')?.remove();
  }, []);

  return <>{children}</>;
};

const AuthLoader: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const loading = useAuthLoading();
  const initialized = useAuthInitialized();
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const initializationError = useAuthStore((s) => s.initializationError);
  const sessionPlane = useAuthStore((s) => s.sessionPlane);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const failCountRef = useRef(0);
  const sessionPromptOpenRef = useRef(false);
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });

  const initialize = useAuthStore((s) => s.initialize);

  useEffect(() => {
    initialize();
  }, [initialize]);

  const sessionCheck = useCallback(async () => {
    if (!authEnabled) return;
    if (!useAuthStore.getState().userId) return;
    try {
      await useAuthStore.getState().refresh();
      failCountRef.current = 0;
      return;
    } catch (error) {
      if (error instanceof Error && error.message === 'Session expired') {
        window.location.replace(buildLocalLoginUrl(sessionPlane));
        return;
      }
      failCountRef.current += 1;
    }
    if (failCountRef.current >= 2 && !sessionPromptOpenRef.current) {
      sessionPromptOpenRef.current = true;
      Modal.confirm({
        title: t('session.checkFailedTitle'),
        content: t('session.checkFailedContent'),
        okText: t('common.retry'),
        cancelText: t('account.logout'),
        onOk: async () => {
          try {
            await useAuthStore.getState().refresh();
            failCountRef.current = 0;
          } catch {
            window.location.replace(buildLocalLoginUrl(sessionPlane));
          } finally {
            sessionPromptOpenRef.current = false;
          }
        },
        onCancel: async () => {
          try {
            await useAuthStore.getState().logout();
          } catch {
            /* ignore */
          }
          sessionPromptOpenRef.current = false;
          window.location.replace(buildLocalLoginUrl(sessionPlane));
        },
      });
      failCountRef.current = 0;
    }
  }, [authEnabled, sessionPlane, t]);

  useEffect(() => {
    if (!initialized || !authEnabled) return;
    intervalRef.current = setInterval(sessionCheck, 60_000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [initialized, authEnabled, sessionCheck]);

  if (!initialized || loading) {
    if (document.getElementById('initial-loading')) return null;
    return <AppLoading label={t('common.loading')} />;
  }

  if (initializationError) {
    return (
      <InitialLoadingCleanup>
        <div style={{ maxWidth: 640, margin: '15vh auto', padding: 24 }}>
          <Alert
            type="error"
            showIcon
            message={t('session.initializeFailed')}
            description={t('session.initializeFailedDetail')}
            action={
              <Button size="small" onClick={() => void initialize()}>
                {t('common.retry')}
              </Button>
            }
          />
        </div>
      </InitialLoadingCleanup>
    );
  }

  return <InitialLoadingCleanup>{children}</InitialLoadingCleanup>;
};

const App: React.FC = React.memo(() => {
  const { language } = useAppI18n();
  const { colorMode } = useQwenPawDataColorMode();
  const antdLocale = language === 'zh' ? zhCN : enUS;
  const themeProviderProps = useMemo(() => createContextThemeProviderProps(colorMode), [colorMode]);

  return (
    <ConfigProvider {...themeProviderProps} locale={antdLocale}>
      <AntdApp>
        <AuthLoader>
          <RouterProvider router={router} />
        </AuthLoader>
      </AntdApp>
    </ConfigProvider>
  );
});

export default App;
