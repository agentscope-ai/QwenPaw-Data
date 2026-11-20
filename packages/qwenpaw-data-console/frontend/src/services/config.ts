import { getRuntimeConfig } from './runtimeConfig.js';

function getEnv(): Record<string, string | boolean | undefined> | undefined {
  return (
    import.meta as unknown as {
      env?: Record<string, string | boolean | undefined>;
    }
  ).env;
}

function stringValue(value: string | boolean | undefined): string {
  return typeof value === 'string' ? value : '';
}

function getEnvApiBaseUrl(): string {
  return stringValue(getEnv()?.VITE_API_BASE_URL);
}

function getEnvAuthApiBaseUrl(): string {
  const env = getEnv();
  if (env?.DEV === true) return '';
  return stringValue(env?.VITE_AUTH_API_URL) || stringValue(env?.VITE_AUTH_URL);
}

/** 未配置 VITE_*_FRONTEND_URL 时的来源：DEV 分端口本地联调，PROD 同源相对路径。 */
function frontendOriginFallback(): string {
  return getEnv()?.DEV === true ? 'http://localhost:3000' : '';
}

/** Packaged Console runtime config (/console-config.js) wins over build-time env. */
function getRuntimeContextBaseUrl(): string {
  const { databridge } = getRuntimeConfig() as { databridge?: { base_path?: string } | null };
  return databridge?.base_path || '';
}

export function getApiUrl(path: string): string {
  const base = getEnvApiBaseUrl().replace(/\/$/, '');
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith('/api/')
    ? normalizedPath
    : `/api/v1${normalizedPath}`;
  return `${base}${prefixedPath}`;
}

export function getContextFrontendUrl(path = ''): string {
  const raw = getRuntimeContextBaseUrl() || stringValue(getEnv()?.VITE_CONTEXT_FRONTEND_URL);
  // 设为 "/" → base 为空串，result 为空时返回 "/" 走相对路径
  const base = (raw || frontendOriginFallback()).replace(/\/$/, '');
  const normalizedPath = path ? (path.startsWith('/') ? path : `/${path}`) : '';
  const result = `${base}${normalizedPath}`;
  return result || '/';
}

export function getAuthApiUrl(path: string): string {
  const base = getEnvAuthApiBaseUrl().replace(/\/$/, '');
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith('/api/auth/')
    ? normalizedPath
    : `/api/auth${normalizedPath}`;
  return `${base}${prefixedPath}`;
}

export function getAuthLoginUrl(redirectUrl: string): string {
  const env = getEnv();
  const base = (
    stringValue(env?.VITE_AUTH_FRONTEND_URL) ||
    getRuntimeContextBaseUrl() ||
    stringValue(env?.VITE_CONTEXT_FRONTEND_URL) ||
    frontendOriginFallback()
  ).replace(/\/$/, '');
  return `${base}/login?redirect=${encodeURIComponent(redirectUrl)}`;
}
