import { getApiUrl, getAuthApiUrl, getAuthLoginUrl } from './config.js';

interface AuthStatusResponse {
  enabled: boolean; // 是否启用登录认证。
  sso_enabled: boolean; // 是否启用单点登录。
  sso_login_path: string; // 单点登录跳转地址。
}

interface AuthenticatedFetchOptions {
  redirectOnUnauthorized?: boolean; // 刷新登录态后仍为 401 时是否跳转登录页。
}

export interface HostWorkspaceInfo {
  id: string; // 工作空间 ID。
  name: string; // 工作空间名称。
}

export interface HostCurrentUser {
  user_id: string; // 当前登录用户 ID。
  username: string; // 当前登录用户名。
  display_name?: string; // 用户显示名称，缺失时回退用户名。
  tenant_id: string | null; // 当前租户 ID，平台管理员可能为 null。
  workspaces: HostWorkspaceInfo[]; // 当前用户可访问的工作空间列表。
  active_workspace_id: string; // 当前工作空间 ID，未选择时为空字符串。
  active_workspace_name: string; // 当前工作空间名称。
}

let refreshInFlight: Promise<boolean> | null = null;
let ssoLoginPath: string | null = null;
let currentUser: HostCurrentUser | null = null;

let authContext = { revision: 0, ready: false };
const authListeners = new Set<() => void>();

export function getHostAuthContext() {
  return authContext;
}

export function subscribeHostAuth(listener: () => void) {
  authListeners.add(listener);
  return () => {
    authListeners.delete(listener);
  };
}

function publishAuthContext(ready: boolean, invalidate = false) {
  authContext = { revision: authContext.revision + Number(invalidate), ready };
  authListeners.forEach((listener) => listener());
  return authContext.revision;
}

export function getHostCurrentUser(): HostCurrentUser | null {
  return currentUser;
}

async function performRefresh(): Promise<boolean> {
  try {
    const response = await fetch(getAuthApiUrl('/refresh'), {
      method: 'POST',
      credentials: 'include',
    });
    return response.ok;
  } catch {
    return false;
  }
}

export function refreshAuthSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

export function redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  if (ssoLoginPath) {
    const redirect = encodeURIComponent(window.location.href);
    window.location.assign(`${ssoLoginPath}?back_url=${redirect}`);
    return;
  }
  window.location.assign(getAuthLoginUrl(window.location.href));
}

export async function fetchWithAuthRetry(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: AuthenticatedFetchOptions = {},
): Promise<Response> {
  const fetchOptions: RequestInit = {
    ...init,
    credentials: 'include',
  };
  let response = await fetch(input, fetchOptions);
  if (response.status !== 401) return response;

  if (await refreshAuthSession()) {
    response = await fetch(input, fetchOptions);
  }

  if (response.status === 401 && options.redirectOnUnauthorized !== false) {
    redirectToLogin();
  }
  return response;
}

export async function bootstrapHostAuth(): Promise<boolean> {
  currentUser = null;
  const revision = publishAuthContext(false, true);
  const statusResponse = await fetch(getApiUrl('/auth/status'), {
    credentials: 'include',
  });
  if (!statusResponse.ok) {
    throw new Error(
      `Auth status request failed: ${statusResponse.status} ${statusResponse.statusText}`,
    );
  }

  const status = (await statusResponse.json()) as AuthStatusResponse;
  ssoLoginPath = status.sso_enabled ? status.sso_login_path : null;
  if (revision !== authContext.revision) return false;
  if (!status.enabled) {
    publishAuthContext(true);
    return true;
  }

  const meResponse = await fetchWithAuthRetry(getAuthApiUrl('/me'));
  if (meResponse.status === 401) return false;
  if (!meResponse.ok) {
    throw new Error(`Current user request failed: ${meResponse.status} ${meResponse.statusText}`);
  }
  const user = (await meResponse.json()) as HostCurrentUser;
  if (revision !== authContext.revision) return false;
  currentUser = user;
  publishAuthContext(true);
  return true;
}

export async function switchHostWorkspace(workspaceId: string): Promise<void> {
  currentUser = null;
  const revision = publishAuthContext(false, true);
  try {
    const response = await fetchWithAuthRetry(getAuthApiUrl('/switch-workspace'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace_id: workspaceId }),
    });
    if (response.ok) return;

    let detail = '';
    try {
      const payload = (await response.json()) as { detail?: unknown };
      detail = typeof payload.detail === 'string' ? payload.detail : '';
    } catch {
      // Fall through to the HTTP status when the Auth response is not JSON.
    }
    throw new Error(detail || `${response.status} ${response.statusText}`.trim());
  } catch (error) {
    // A failed switch cannot restore cached permissions without confirming /me.
    if (revision === authContext.revision) await bootstrapHostAuth().catch(() => false);
    throw error;
  }
}

export async function logoutHostSession(): Promise<void> {
  currentUser = null;
  publishAuthContext(false, true);
  try {
    await fetch(getAuthApiUrl('/logout'), {
      method: 'POST',
      credentials: 'include',
    });
  } finally {
    currentUser = null;
  }
}
