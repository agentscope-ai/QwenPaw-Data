/**
 * AuthGuard — route-level authentication guard.
 *
 * Flow:
 * 1. authEnabled=false → render children immediately
 * 2. Call GET /api/auth/me (cookie auto-sent)
 *    - Success → populate AuthState, render children
 *    - 401 → try POST /api/auth/refresh
 *      - Refresh success → retry /api/auth/me
 *      - Refresh fail → redirect to Auth /login
 * 3. Multi-origin: check URL ?code= → call exchangeCode() → retry /api/auth/me
 */

import React, { useEffect, useState, useCallback } from 'react';
import type { AuthState, MeResponse } from './types';
import { fetchCurrentUser, refreshToken, exchangeCode } from './auth';
import { buildLoginUrl } from './config';

function meToAuthState(me: MeResponse): AuthState {
  return {
    isAuthenticated: true,
    userId: me.user_id,
    username: me.username,
    displayName: me.display_name || me.username,
    tenantId: me.tenant_id,
    role: me.role,
    workspaceIds: me.workspace_ids,
    activeWorkspaceId: me.active_workspace_id,
    activeWorkspaceName: me.active_workspace_name,
    isAdmin: me.role !== 'member',
    isTenantAdmin: me.role === 'tenant_admin',
    isPlatformAdmin: me.role === 'platform_admin',
  };
}

export interface AuthGuardProps {
  children: React.ReactNode;
  authEnabled: boolean;
  onAuthStateChange?: (state: AuthState) => void;
  loadingElement?: React.ReactNode;
}

export function AuthGuard({
  children,
  authEnabled,
  onAuthStateChange,
  loadingElement,
}: AuthGuardProps): React.ReactElement | null {
  const [loading, setLoading] = useState(authEnabled);
  const [authenticated, setAuthenticated] = useState(!authEnabled);

  const resolve = useCallback(async () => {
    if (!authEnabled) return;

    const params = new URLSearchParams(window.location.search);
    const code = params.get('code');
    if (code) {
      try {
        await exchangeCode(code, window.location.origin + window.location.pathname);
        params.delete('code');
        const cleanUrl = window.location.pathname + (params.toString() ? `?${params}` : '');
        window.history.replaceState(null, '', cleanUrl);
      } catch {
        // code exchange failed — continue to normal auth check
      }
    }

    let me = await fetchCurrentUser();
    if (!me) {
      const refreshed = await refreshToken();
      if (refreshed) {
        me = await fetchCurrentUser();
      }
    }

    if (me) {
      const state = meToAuthState(me);
      onAuthStateChange?.(state);
      setAuthenticated(true);
    } else {
      window.location.href = buildLoginUrl();
      return;
    }

    setLoading(false);
  }, [authEnabled, onAuthStateChange]);

  useEffect(() => {
    resolve();
  }, [resolve]);

  if (loading) {
    return (loadingElement as React.ReactElement) ?? null;
  }
  if (!authenticated) return null;
  return React.createElement(React.Fragment, null, children);
}
