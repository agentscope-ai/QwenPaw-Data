import { useEffect, useMemo, type ReactNode } from 'react';
import { I18nextProvider } from 'react-i18next';
import { App as AntdApp, Menu } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import { ConfigProvider } from '@/design';
import { useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import { createContextThemeProviderProps } from '@/theme';
import { AuthApiError } from '@/auth';
import i18n from '@/i18n';
import '../index.css';
import { useAuthStore, getSessionPlaneForPath } from '@/store/authStore';
import { asideMenuConfig } from '@/router/menuConfig';
export { createContextRoutes } from '@/router';
export { configureEmbedding, resolveCmUrl } from './context';
export { configureUiBridge } from './bridge';
export { getSessionPlaneForPath };

export function authPlane(path: string) {
  if (
    [
      '/login',
      '/platform/login',
      '/register',
      '/onboarding',
      '/choose-account',
      '/auth/odps/result',
    ].includes(path)
  )
    return 'public';
  return getSessionPlaneForPath(path);
}
let initializing: Promise<void> | undefined;
export function initializeContextAuth() {
  if (!initializing) {
    useAuthStore.getState().clear();
    initializing = useAuthStore
      .getState()
      .initialize()
      .finally(() => {
        initializing = undefined;
      });
  }
  return initializing;
}
export const getContextAuth = useAuthStore.getState;
export const subscribeContextAuth = useAuthStore.subscribe;
export async function refreshContextAuth() {
  const state = useAuthStore.getState();
  if (!state.authEnabled || !state.userId) return;
  try {
    await state.refresh();
  } catch (error) {
    if (error instanceof AuthApiError && (error.status === 401 || error.status === 403))
      return 'unauthorized' as const;
    throw error;
  }
}

export function ContextProvider({ children, language }: { children: ReactNode; language: string }) {
  const { colorMode } = useQwenPawDataColorMode();
  const theme = useMemo(() => createContextThemeProviderProps(colorMode), [colorMode]);
  useEffect(() => {
    void i18n.changeLanguage(language);
  }, [language]);
  return (
    <I18nextProvider i18n={i18n}>
      <ConfigProvider {...theme}>
        <AntdApp className="cm-embedded">{children}</AntdApp>
      </ConfigProvider>
    </I18nextProvider>
  );
}

/** Reuses the CM menu and permission matrix; the Host owns the only visible sidebar. */
export function ContextMenu({ language }: { language: string }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const state = useAuthStore();
  const role = state.role === 'platform_admin' ? state.role : state.activeWorkspaceRole;
  type Item = (typeof asideMenuConfig)[number];
  const convert = (items: Item[]): import('antd').MenuProps['items'] =>
    items
      .filter(
        (item) =>
          (!item.requiredRoles || (state.authEnabled && item.requiredRoles.includes(role))) &&
          (!state.authEnabled ||
            !item.requiredPermission ||
            state.capabilities.includes(item.requiredPermission)),
      )
      .map((item) => ({
        key: item.children
          ? `group:${item.key || item.nameKey}`
          : item.path || item.key || item.nameKey,
        label: i18n.t(item.nameKey, { lng: language }),
        icon: item.icon,
        children: item.children ? convert(item.children) : undefined,
      }));
  return (
    <Menu
      mode="inline"
      selectedKeys={[pathname]}
      items={convert(asideMenuConfig)}
      onClick={({ key }) => {
        if (key.startsWith('/')) navigate(key);
      }}
    />
  );
}
