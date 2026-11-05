/** Optional deployment settings supplied by the packaged local Console. */
interface ContextConsoleConfig {
  base_path: string;
  api_base_url: string;
  auth_mode: 'portal';
  console_url: string;
}

declare global {
  interface Window {
    QWENPAW_DATA_CONSOLE?: { databridge?: ContextConsoleConfig | null };
  }
}

export function getConsoleConfig(): ContextConsoleConfig | undefined {
  return window.QWENPAW_DATA_CONSOLE?.databridge ?? undefined;
}

export function getContextBasePath(): string {
  return getConsoleConfig()?.base_path || import.meta.env.BASE_URL.replace(/\/$/, '') || '/';
}
