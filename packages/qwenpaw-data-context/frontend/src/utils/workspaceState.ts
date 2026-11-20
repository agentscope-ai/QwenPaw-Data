export const WORKSPACE_CHANGED_EVENT = 'dataaxon:workspace-changed';

function isGraphStateForWorkspace(key: string, workspaceId?: string): boolean {
  if (!key.startsWith('qwenpaw-data:workspace:')) return false;
  if (!workspaceId) return true;
  const [, , , keyWorkspaceId] = key.split(':');
  return keyWorkspaceId === workspaceId;
}

export function clearWorkspaceScopedState(workspaceId?: string): void {
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index);
      if (!key) continue;
      if (
        key.startsWith('dataaxon:workspace:') ||
        key.startsWith('dataaxon-cache:') ||
        isGraphStateForWorkspace(key, workspaceId)
      ) {
        storage.removeItem(key);
      }
    }
  }
  window.dispatchEvent(new CustomEvent(WORKSPACE_CHANGED_EVENT));
  window.dispatchEvent(new CustomEvent('dataaxon:mcp-disconnect'));
}
