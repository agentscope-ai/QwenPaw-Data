const AUTH_TOKEN_KEY = "datapaw_auth_token";
export const AUTH_CHANGE_EVENT = "datapaw-auth-change";

function getEnvApiBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL ?? "";
}

export function getApiUrl(path: string): string {
  const base = getEnvApiBaseUrl().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith("/api/")
    ? normalizedPath
    : `/api${normalizedPath}`;
  return `${base}${prefixedPath}`;
}

export function getApiToken(): string {
  const sessionToken = sessionStorage.getItem(AUTH_TOKEN_KEY) ?? "";
  if (sessionToken) return sessionToken;

  // One-time migration from older builds. Remove the persistent copy as soon
  // as it is observed, even if sessionStorage is later cleared.
  const legacyToken = localStorage.getItem(AUTH_TOKEN_KEY) ?? "";
  if (legacyToken) {
    sessionStorage.setItem(AUTH_TOKEN_KEY, legacyToken);
    localStorage.removeItem(AUTH_TOKEN_KEY);
  }
  return legacyToken;
}

export function setAuthToken(token: string): void {
  const normalized = token.trim();
  if (!normalized) {
    clearAuthToken();
    return;
  }
  sessionStorage.setItem(AUTH_TOKEN_KEY, normalized);
  localStorage.removeItem(AUTH_TOKEN_KEY);
  window.dispatchEvent(new Event(AUTH_CHANGE_EVENT));
}

export function clearAuthToken(): void {
  sessionStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_TOKEN_KEY);
  window.dispatchEvent(new Event(AUTH_CHANGE_EVENT));
}
