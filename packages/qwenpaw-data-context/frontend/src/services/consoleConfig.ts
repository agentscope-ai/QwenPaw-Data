/** Optional deployment settings supplied by the packaged local Console. */
interface ContextConsoleConfig {
  base_path: string;
  api_base_url: string;
  auth_mode: 'portal';
  console_url: string;
}

declare global {
  interface Window {
    QWENPAW_DATA_CONSOLE?: { console_protocol?: number; databridge?: ContextConsoleConfig | null };
  }
}

export function getConsoleConfig(): ContextConsoleConfig | undefined {
  return typeof window === 'undefined' ? undefined : window.QWENPAW_DATA_CONSOLE?.databridge ?? undefined;
}

export function isConsolePortal(): boolean {
  return typeof window !== 'undefined' && window.QWENPAW_DATA_CONSOLE?.console_protocol === 1;
}

export function getContextBasePath(): string {
  return getConsoleConfig()?.base_path || import.meta.env.BASE_URL.replace(/\/$/, '') || '/';
}
