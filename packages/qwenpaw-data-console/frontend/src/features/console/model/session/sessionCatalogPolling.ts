export const SESSION_CATALOG_POLL_INTERVAL_MS = 30_000;

export function shouldRefreshSessionCatalog(visibilityState: string): boolean {
  return visibilityState === 'visible';
}
