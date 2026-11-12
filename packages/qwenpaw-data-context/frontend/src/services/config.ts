import { getConsoleConfig } from './consoleConfig';

function getEnvApiBaseUrl(): string {
  return getConsoleConfig()?.api_base_url ?? import.meta.env.VITE_API_BASE_URL ?? '';
}

export function getApiUrl(path: string): string {
  const base = getEnvApiBaseUrl().replace(/\/$/, '');
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith('/api/')
    ? normalizedPath
    : `/api${normalizedPath}`;
  return `${base}${prefixedPath}`;
}
