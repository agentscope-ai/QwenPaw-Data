/** Central URL discovery for every frontend. */
import { getConsoleConfig } from '@/services/consoleConfig';
import { isEmbedded } from '../embedded/context';

function runtimeValue(name: string): string {
  if (typeof window !== 'undefined') {
    const value = (window as unknown as Record<string, unknown>)[`__${name}__`];
    if (typeof value === 'string' && value) return value;
  }
  try {
    const value = (import.meta as unknown as { env?: Record<string, string> }).env?.[name];
    if (value) return value;
  } catch {
    // Non-Vite consumer.
  }
  return '';
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '');
}

export function getAuthApiUrl(): string {
  if (isEmbedded()) return '';
  return trimTrailingSlash(runtimeValue('VITE_AUTH_API_URL') || runtimeValue('VITE_AUTH_URL'));
}

/** @deprecated Use getAuthApiUrl; retained for compatibility. */
export const getAuthBaseUrl = getAuthApiUrl;

export function getAuthFrontendUrl(): string {
  return trimTrailingSlash(runtimeValue('VITE_AUTH_FRONTEND_URL') || getAuthApiUrl());
}

export function getContextFrontendUrl(): string {
  return trimTrailingSlash(
    runtimeValue('VITE_CONTEXT_FRONTEND_URL') || runtimeValue('VITE_DEFAULT_APP_URL') || '/',
  );
}

/**
 * 智能体中心（Host / Data Console）地址；未部署时返回空串。
 *
 * 不能像其他 getter 那样兜底成 `'/'`：trimTrailingSlash 会把唯一的斜杠也剪掉，
 * 结果是空串被当成合法地址用于跳转（`window.location.href = ""` → 空白页）。
 * 这里明确返回空串，由调用方据此隐藏入口 —— 没有部署 Host 就不该出现这个入口。
 */
export function getHostFrontendUrl(): string {
  if (isEmbedded()) return '/console';
  // Packaged console: the portal-provided console URL is the Host entry.
  const consoleUrl = typeof window === 'undefined' ? '' : getConsoleConfig()?.console_url;
  return trimTrailingSlash(consoleUrl || runtimeValue('VITE_HOST_FRONTEND_URL'));
}

/** Unified model settings require an explicitly configured Host deployment. */
export function resolveHostModelConfigUrl(): string {
  if (isEmbedded()) return '/console/settings/agent';
  const base = getHostFrontendUrl();
  if (!base || typeof window === 'undefined') return '';
  try {
    const url = new URL(base, window.location.origin);
    if (!['http:', 'https:'].includes(url.protocol)) return '';
    const path = url.pathname.replace(/\/+$/, '');
    url.pathname = `${path.endsWith('/console') ? path : `${path}/console`}/settings/agent`;
    url.search = '';
    url.hash = '';
    return url.toString();
  } catch {
    return '';
  }
}

export function currentPageDatasourceId(): string {
  if (typeof window === 'undefined') return '';
  return (new URLSearchParams(window.location.search).get('datasource_id') || '').trim();
}

/** Host console URL. Never use Context `/` for platform_admin — that redirects back here. */
export function resolveAgentCenterUrl(): string {
  if (isEmbedded()) return '/console';
  const configured = getHostFrontendUrl();
  let base = configured;
  if (!base || base === '/') {
    if (
      typeof window !== 'undefined' &&
      /^(localhost|127\.0\.0\.1)$/.test(window.location.hostname)
    ) {
      base = 'http://localhost:3100';
    } else {
      base = configured || '/';
    }
  }
  const datasourceId = currentPageDatasourceId();
  if (!datasourceId || typeof window === 'undefined') {
    return base;
  }
  try {
    const url = new URL(base, window.location.origin);
    if (!url.pathname || url.pathname === '/') {
      url.pathname = '/console';
    }
    url.searchParams.set('datasource_id', datasourceId);
    return url.toString();
  } catch {
    return base;
  }
}

export function isAgentCenterSameApp(url: string): boolean {
  if (!url || url === '/') return true;
  if (typeof window === 'undefined') return false;
  try {
    return new URL(url, window.location.origin).origin === window.location.origin;
  } catch {
    return true;
  }
}

export function getConfiguredRedirectOrigins(): string[] {
  const configured = runtimeValue('VITE_AUTH_REDIRECT_ORIGINS')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  const appUrls = [getAuthFrontendUrl(), getContextFrontendUrl(), getHostFrontendUrl()];
  const origins = appUrls.flatMap((value) => {
    try {
      return [
        new URL(value, typeof window === 'undefined' ? 'http://localhost' : window.location.origin)
          .origin,
      ];
    } catch {
      return [];
    }
  });
  return [...new Set([...configured, ...origins])];
}

export function getConfiguredRedirectPaths(): string[] {
  const paths = runtimeValue('VITE_AUTH_REDIRECT_PATHS')
    .split(',')
    .map((value) => value.trim())
    .filter((value) => value.startsWith('/'));
  return paths.length ? paths : ['/'];
}

export function buildLoginUrl(redirectUrl?: string): string {
  const base = getAuthFrontendUrl();
  const redirect = redirectUrl || (typeof window !== 'undefined' ? window.location.href : '/');
  return `${base}/login?redirect=${encodeURIComponent(redirect)}`;
}

export function buildAuthApiUrl(path: string): string {
  const portal = typeof window === 'undefined' ? undefined : getConsoleConfig();
  const base = !isEmbedded() && portal?.auth_mode === 'portal' ? trimTrailingSlash(portal.api_base_url) : getAuthApiUrl();
  return `${base}${path.startsWith('/') ? path : `/${path}`}`;
}
