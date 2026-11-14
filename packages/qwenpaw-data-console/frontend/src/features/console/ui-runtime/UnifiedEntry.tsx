import { useEffect, useLayoutEffect, useState } from 'react';
import { Outlet, Navigate, useLocation } from 'react-router-dom';
import {
  authPlane,
  getContextAuth,
  initializeContextAuth,
  refreshContextAuth,
  subscribeContextAuth,
  ContextProvider,
} from '@qwenpaw-data/cm';
import { adoptUnifiedIdentity } from '../../../services/auth';
import { useAppI18n } from '../../../i18n';

export function UnifiedEntry() {
  useLayoutEffect(() => {
    document.getElementById('initial-loading')?.remove();
  }, []);
  const location = useLocation();
  const plane = authPlane(location.pathname);
  const [readyPlane, setReadyPlane] = useState<string | null>(null);
  const [state, setState] = useState(getContextAuth);
  useEffect(() => {
    let active = true;
    setReadyPlane(null);
    adoptUnifiedIdentity(null);
    const unsubscribe = subscribeContextAuth((next) => {
      if (active) {
        setState(next);
        adoptUnifiedIdentity(next);
      }
    });
    void initializeContextAuth().then(() => {
      if (!active) return;
      const next = getContextAuth();
      setState(next);
      adoptUnifiedIdentity(next);
      setReadyPlane(plane);
    });
    return () => {
      active = false;
      unsubscribe();
      adoptUnifiedIdentity(null);
    };
  }, [plane]);
  useEffect(() => {
    if (plane === 'public' || readyPlane !== plane) return;
    let active = true;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        if ((await refreshContextAuth()) === 'unauthorized' && active) {
          adoptUnifiedIdentity(null);
          setState((old) => ({ ...old, initializationError: 'AUTH_REFRESH_FAILED' }));
        }
      } catch {
        // A transient failure is retried on focus or at the next interval.
      } finally {
        refreshing = false;
      }
    };
    const timer = setInterval(refresh, 60000);
    window.addEventListener('focus', refresh);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener('focus', refresh);
    };
  }, [plane, readyPlane]);
  if (readyPlane !== plane || state.loading) return <div role="status">正在初始化身份…</div>;
  if (state.initializationError) return <div role="alert">无法确认身份与权限，请刷新后重试。</div>;
  if (
    plane === 'tenant' &&
    state.authEnabled &&
    state.mustChangePassword &&
    location.pathname !== '/user-center/account/change-password'
  ) {
    return (
      <Navigate
        replace
        to={`/user-center/account/change-password?redirect=${encodeURIComponent(location.pathname + location.search)}`}
      />
    );
  }
  return <Outlet />;
}
export function EmbeddedContext({ children }: { children: import('react').ReactNode }) {
  const { language } = useAppI18n();
  return <ContextProvider language={language}>{children}</ContextProvider>;
}
