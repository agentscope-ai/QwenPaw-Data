/**
 * Auth API call wrappers.
 *
 * All requests use ``credentials: 'include'`` so the browser automatically
 * sends httpOnly cookies. No token is read from or written to localStorage.
 */

import type {
  AccessRequest,
  AccessRequestStatus,
  AccessRequestTargetType,
  AuthStatusResponse,
  ChangePasswordRequest,
  ChooserStatus,
  LoginResponse,
  MeResponse,
  OnboardingStatus,
  PlatformLoginResponse,
  RawCypherGrantRequest,
  RawCypherGrantResponse,
} from './types';
import { buildAuthApiUrl, buildLoginUrl, getAuthFrontendUrl } from './config';

export type AuthSessionPlane = 'public' | 'tenant' | 'platform';
export type CurrentUserSession = Exclude<AuthSessionPlane, 'public'> | 'auto';

export interface AuthRequestOptions {
  session?: AuthSessionPlane;
  retryOnUnauthorized?: boolean;
  redirectOnUnauthorized?: boolean;
}

interface AuthErrorBody {
  detail?: unknown;
  retry_after?: unknown;
  code?: unknown;
  message?: unknown;
}

/** FastAPI returns a string `detail` for handled errors but an array of
 * validation items for 422; flatten both into one readable message. */
export function formatDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        const { loc, msg } = (item ?? {}) as { loc?: unknown[]; msg?: string };
        if (!msg) return '';
        const field = Array.isArray(loc) ? loc.filter((part) => part !== 'body').join('.') : '';
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (messages.length) return messages.join('; ');
  }
  return '';
}

export class AuthApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly code?: string;
  readonly retryAfter?: number;

  constructor(
    message: string,
    options: {
      status: number;
      body?: unknown;
      code?: string;
      retryAfter?: number;
    },
  ) {
    super(message);
    this.name = 'AuthApiError';
    this.status = options.status;
    this.body = options.body;
    this.code = options.code;
    this.retryAfter = options.retryAfter;
  }
}

const PUBLIC_TENANT_PATHS = new Set([
  '/api/auth/status',
  '/api/auth/login',
  '/api/auth/register',
  '/api/auth/refresh',
  '/api/auth/exchange',
  '/api/auth/onboarding',
  '/api/auth/chooser',
  '/api/auth/chooser/select',
]);

/** Onboarding sub-paths are parameterised, so prefix-match them: the applicant
 * holds only the onboarding cookie and a 401 there means "sign in again", not
 * "refresh the tenant session". */
function isPublicTenantPath(path: string): boolean {
  return PUBLIC_TENANT_PATHS.has(path) || path.startsWith('/api/auth/onboarding/');
}

function inferSession(path: string): AuthSessionPlane {
  if (path.startsWith('/api/platform/')) return 'platform';
  if (isPublicTenantPath(path)) return 'public';
  return 'tenant';
}

function getErrorMessage(body: AuthErrorBody, status: number): string {
  const detail = formatDetail(body.detail);
  const message =
    detail || (typeof body.message === 'string' ? body.message : '') || `Auth API error ${status}`;
  const retryAfter = Number(body.retry_after);
  if (status === 429 && Number.isFinite(retryAfter) && retryAfter > 0) {
    return `${message} (${retryAfter}s)`;
  }
  return message;
}

async function readAuthError(response: Response): Promise<AuthApiError> {
  const body = (await response.json().catch(() => ({}))) as AuthErrorBody;
  const retryAfterValue = Number(body.retry_after);
  return new AuthApiError(getErrorMessage(body, response.status), {
    status: response.status,
    body,
    code: typeof body.code === 'string' ? body.code : undefined,
    retryAfter: Number.isFinite(retryAfterValue) ? retryAfterValue : undefined,
  });
}

function buildHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers);
  const body = init?.body;
  if (
    body != null &&
    !(typeof FormData !== 'undefined' && body instanceof FormData) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json');
  }
  return headers;
}

async function performAuthRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(buildAuthApiUrl(path), {
    ...init,
    credentials: 'include',
    headers: buildHeaders(init),
  });
  if (!response.ok) throw await readAuthError(response);
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    return (await response.text()) as T;
  }
  return response.json();
}

function redirectToSessionLogin(session: AuthSessionPlane): void {
  if (typeof window === 'undefined') return;
  if (session === 'platform') {
    const base = getAuthFrontendUrl();
    const redirect = encodeURIComponent(window.location.href);
    window.location.assign(`${base}/platform/login?redirect=${redirect}`);
    return;
  }
  if (ssoLoginPath) {
    const redirect = encodeURIComponent(window.location.href);
    window.location.assign(`${ssoLoginPath}?back_url=${redirect}`);
    return;
  }
  window.location.assign(buildLoginUrl());
}

/**
 * Unified Auth/control-plane client.
 *
 * Protected tenant requests share a single refresh request and retry at most
 * once. Platform requests never attempt a tenant refresh.
 */
export async function authFetch<T>(
  path: string,
  init?: RequestInit,
  options: AuthRequestOptions = {},
): Promise<T> {
  const session = options.session ?? inferSession(path);
  const retryOnUnauthorized =
    options.retryOnUnauthorized ?? (session === 'tenant' && !isPublicTenantPath(path));
  const redirectOnUnauthorized = options.redirectOnUnauthorized ?? session !== 'public';

  try {
    return await performAuthRequest<T>(path, init);
  } catch (error) {
    if (
      error instanceof AuthApiError &&
      error.status === 401 &&
      session === 'tenant' &&
      retryOnUnauthorized
    ) {
      const refreshed = await refreshToken();
      if (refreshed) {
        try {
          return await performAuthRequest<T>(path, init);
        } catch (retryError) {
          if (
            retryError instanceof AuthApiError &&
            retryError.status === 401 &&
            redirectOnUnauthorized
          ) {
            redirectToSessionLogin(session);
          }
          throw retryError;
        }
      }
    }
    if (error instanceof AuthApiError && error.status === 401 && redirectOnUnauthorized) {
      redirectToSessionLogin(session);
    }
    throw error;
  }
}

export async function fetchAuthStatus(): Promise<AuthStatusResponse> {
  const status = await authFetch<AuthStatusResponse>('/api/auth/status');
  ssoLoginPath = status.sso_enabled ? status.sso_login_path : null;
  return status;
}

export async function fetchOnboardingStatus(): Promise<OnboardingStatus> {
  return authFetch<OnboardingStatus>('/api/auth/onboarding', undefined, {
    session: 'public',
    redirectOnUnauthorized: false,
  });
}

export async function fetchChooserStatus(): Promise<ChooserStatus> {
  return authFetch<ChooserStatus>('/api/auth/chooser', undefined, {
    session: 'public',
    redirectOnUnauthorized: false,
  });
}

export async function selectChooserAccount(id: string): Promise<{ ok: boolean; landing: string }> {
  return authFetch<{ ok: boolean; landing: string }>(
    '/api/auth/chooser/select',
    {
      method: 'POST',
      body: JSON.stringify({ id }),
    },
    {
      session: 'public',
      redirectOnUnauthorized: false,
    },
  );
}

export async function submitAccessRequest(body: {
  target_type: AccessRequestTargetType;
  target_id: string;
  message?: string;
}): Promise<{ id: string; status: string }> {
  return authFetch<{ id: string; status: string }>(
    '/api/auth/onboarding/requests',
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
    {
      session: 'public',
      redirectOnUnauthorized: false,
    },
  );
}

export async function cancelAccessRequest(requestId: string): Promise<void> {
  await authFetch(
    `/api/auth/onboarding/requests/${encodeURIComponent(requestId)}/cancel`,
    { method: 'POST' },
    { session: 'public', redirectOnUnauthorized: false },
  );
}

export async function listAccessRequests(
  status: AccessRequestStatus = 'pending',
): Promise<AccessRequest[]> {
  const response = await authFetch<{ requests: AccessRequest[] }>(
    `/api/access-requests?status=${encodeURIComponent(status)}`,
    undefined,
    { session: 'tenant', redirectOnUnauthorized: true },
  );
  return response.requests || [];
}

export async function approveAccessRequest(requestId: string, workspaceId?: string): Promise<void> {
  await authFetch(
    `/api/access-requests/${encodeURIComponent(requestId)}/approve`,
    {
      method: 'POST',
      body: JSON.stringify(workspaceId ? { workspace_id: workspaceId } : {}),
    },
    { session: 'tenant', redirectOnUnauthorized: true },
  );
}

export async function rejectAccessRequest(requestId: string, comment: string): Promise<void> {
  await authFetch(
    `/api/access-requests/${encodeURIComponent(requestId)}/reject`,
    { method: 'POST', body: JSON.stringify({ comment }) },
    { session: 'tenant', redirectOnUnauthorized: true },
  );
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  return authFetch<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export async function register(username: string, password: string): Promise<LoginResponse> {
  return authFetch<LoginResponse>('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

async function fetchMe(
  path: string,
  session: Exclude<AuthSessionPlane, 'public'>,
): Promise<MeResponse | null> {
  try {
    return await authFetch<MeResponse>(path, undefined, {
      session,
      redirectOnUnauthorized: false,
    });
  } catch (error) {
    if (error instanceof AuthApiError && error.status === 401) return null;
    throw error;
  }
}

export function fetchTenantCurrentUser(): Promise<MeResponse | null> {
  return fetchMe('/api/auth/me', 'tenant');
}

export function fetchPlatformCurrentUser(): Promise<MeResponse | null> {
  return fetchMe('/api/platform/me', 'platform');
}

/** Resolve the current user: an explicit session, or tenant then platform. */
export async function fetchCurrentUser(
  session: CurrentUserSession = 'auto',
): Promise<MeResponse | null> {
  if (session === 'tenant') return fetchTenantCurrentUser();
  if (session === 'platform') return fetchPlatformCurrentUser();
  return (await fetchTenantCurrentUser()) ?? fetchPlatformCurrentUser();
}

export async function platformBootstrap(
  bootstrapToken: string,
  username: string,
  password: string,
): Promise<PlatformLoginResponse> {
  return authFetch<PlatformLoginResponse>(
    '/api/platform/bootstrap',
    {
      method: 'POST',
      headers: { 'X-QwenPaw-Data-Bootstrap-Token': bootstrapToken },
      body: JSON.stringify({ username, password }),
    },
    { session: 'public' },
  );
}

export async function platformLogin(
  username: string,
  password: string,
): Promise<PlatformLoginResponse> {
  return authFetch<PlatformLoginResponse>(
    '/api/platform/login',
    {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    },
    { session: 'public' },
  );
}

export async function platformLogout(): Promise<void> {
  await authFetch(
    '/api/platform/logout',
    { method: 'POST' },
    {
      session: 'platform',
      redirectOnUnauthorized: false,
    },
  );
}

export async function switchWorkspace(workspaceId: string): Promise<void> {
  await authFetch(
    '/api/auth/switch-workspace',
    {
      method: 'POST',
      body: JSON.stringify({ workspace_id: workspaceId }),
    },
    { session: 'tenant', redirectOnUnauthorized: true },
  );
}

export async function changePassword(body: ChangePasswordRequest): Promise<void> {
  await authFetch(
    '/api/auth/change-password',
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
    { session: 'tenant', redirectOnUnauthorized: true },
  );
}

export async function issueRawCypherGrant(
  body: RawCypherGrantRequest,
): Promise<RawCypherGrantResponse> {
  return authFetch<RawCypherGrantResponse>(
    '/api/platform/raw-cypher-grants',
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
    { session: 'platform', redirectOnUnauthorized: true },
  );
}

export async function revokeRawCypherGrant(grantId: string): Promise<void> {
  await authFetch(
    `/api/platform/raw-cypher-grants/${encodeURIComponent(grantId)}`,
    {
      method: 'DELETE',
    },
    { session: 'platform', redirectOnUnauthorized: true },
  );
}

export async function logout(): Promise<void> {
  await authFetch(
    '/api/auth/logout',
    { method: 'POST' },
    {
      session: 'tenant',
      redirectOnUnauthorized: false,
    },
  );
}

let refreshInFlight: Promise<boolean> | null = null;
let ssoLoginPath: string | null = null;

export async function refreshToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performAuthRequest('/api/auth/refresh', { method: 'POST' })
      .then(() => true)
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

export async function exchangeCode(code: string, redirectUri: string): Promise<void> {
  await authFetch('/api/auth/exchange', {
    method: 'POST',
    body: JSON.stringify({ code, redirect_uri: redirectUri }),
  });
}
