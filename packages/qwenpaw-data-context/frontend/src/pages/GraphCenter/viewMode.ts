/** Page-local Dev / business view mode for /graph-center/global (design 16). */

export type GraphViewMode = 'dev' | 'biz';

export const GRAPH_VIEW_MODE_STORAGE_KEY = 'graph-center.global.mode';

export function parseViewMode(value: string | null | undefined): GraphViewMode | null {
  return value === 'dev' || value === 'biz' ? value : null;
}

export function readStoredViewMode(): GraphViewMode | null {
  try {
    return parseViewMode(window.localStorage.getItem(GRAPH_VIEW_MODE_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function persistViewMode(mode: GraphViewMode): void {
  try {
    window.localStorage.setItem(GRAPH_VIEW_MODE_STORAGE_KEY, mode);
  } catch {
    /* quota / private mode */
  }
}

export function defaultViewMode(isAdmin: boolean, authEnabled: boolean): GraphViewMode {
  return isAdmin || !authEnabled ? 'dev' : 'biz';
}

export function resolveViewMode(opts: {
  urlMode: string | null;
  isAdmin: boolean;
  authEnabled: boolean;
}): GraphViewMode {
  return (
    parseViewMode(opts.urlMode) ??
    readStoredViewMode() ??
    defaultViewMode(opts.isAdmin, opts.authEnabled)
  );
}
