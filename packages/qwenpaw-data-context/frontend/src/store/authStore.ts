import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { useShallow } from 'zustand/react/shallow';
import type { MeResponse, Resource, Role, TenantRole } from '@/auth';
import {
  fetchAuthStatus,
  fetchPlatformCurrentUser,
  fetchTenantCurrentUser,
  logout as tenantLogout,
  platformLogout,
} from '@/auth';
import { getContextBasePath } from '@/services/consoleConfig';

export type SessionPlane = 'tenant' | 'platform';

/** Routes that must render without a session. */
const PUBLIC_PATHS = ['/login', '/platform/login', '/register', '/onboarding', '/choose-account'];
const PLATFORM_PATHS = ['/platform', '/user-center/tenants', '/user-center/raw-cypher-grants'];
const GRAPH_DATA_PATHS = ['/graph-center', '/graph-management', '/cm-graph'];

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.includes(pathname);
}

function isGraphDataPath(pathname: string): boolean {
  return GRAPH_DATA_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

export function getSessionPlaneForPath(pathname: string): SessionPlane {
  return PLATFORM_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`))
    ? 'platform'
    : 'tenant';
}

/** Same-origin login redirect keeping the current URL as the return target. */
export function buildLocalLoginUrl(plane: SessionPlane = 'tenant'): string {
  const redirect = encodeURIComponent(window.location.href);
  // Router basename (e.g. /databridge in the packaged console) prefixes app routes.
  const basePath = getContextBasePath().replace(/\/$/, '');
  return `${basePath}${plane === 'platform' ? '/platform/login' : '/login'}?redirect=${redirect}`;
}

export function getSafeLocalRedirect(value: string | null | undefined): string {
  if (!value) return '/';
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin ? `${url.pathname}${url.search}${url.hash}` : '/';
  } catch {
    return '/';
  }
}

interface WorkspaceOption {
  id: string;
  name: string;
}

interface AuthStoreState {
  authEnabled: boolean;
  ssoEnabled: boolean;
  ssoLoginPath: string;
  userId: string;
  username: string;
  displayName: string;
  tenantId: string;
  role: Role;
  activeWorkspaceRole: TenantRole;
  capabilities: Resource[];
  mustChangePassword: boolean;
  sessionPlane: SessionPlane;
  workspaceIds: string[];
  workspaces: WorkspaceOption[];
  activeWorkspaceId: string;
  activeWorkspaceName: string;
  isAdmin: boolean;
  isTenantAdmin: boolean;
  isPlatformAdmin: boolean;
  loading: boolean;
  initialized: boolean;
  initializationError: string | null;
  initialize: () => Promise<void>;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  clear: () => void;
}

const initialState = {
  authEnabled: false,
  ssoEnabled: false,
  ssoLoginPath: '',
  userId: '',
  username: '',
  displayName: '',
  tenantId: '',
  role: 'member' as Role,
  activeWorkspaceRole: 'member' as TenantRole,
  capabilities: [] as Resource[],
  mustChangePassword: false,
  sessionPlane: 'tenant' as SessionPlane,
  workspaceIds: [] as string[],
  workspaces: [] as WorkspaceOption[],
  activeWorkspaceId: '',
  activeWorkspaceName: '',
  isAdmin: false,
  isTenantAdmin: false,
  isPlatformAdmin: false,
  loading: true,
  initialized: false,
  initializationError: null as string | null,
};

function requireTenantPermissionFields(me: MeResponse): {
  activeWorkspaceRole: TenantRole;
  capabilities: Resource[];
} {
  if (!me.active_workspace_role || !Array.isArray(me.capabilities)) {
    throw new Error('Active workspace permission context is unavailable');
  }
  return {
    activeWorkspaceRole: me.active_workspace_role,
    capabilities: me.capabilities,
  };
}

function authStateFromMe(me: MeResponse, sessionPlane: SessionPlane) {
  if (sessionPlane === 'platform') {
    if (me.role !== 'platform_admin') {
      throw new Error('Platform session is unavailable');
    }
    return {
      userId: me.user_id,
      username: me.username,
      displayName: me.display_name || me.username,
      tenantId: '',
      role: me.role,
      activeWorkspaceRole: 'member' as TenantRole,
      capabilities: [] as Resource[],
      mustChangePassword: false,
      sessionPlane,
      workspaceIds: [] as string[],
      workspaces: [] as WorkspaceOption[],
      activeWorkspaceId: '',
      activeWorkspaceName: '',
      isAdmin: false,
      isTenantAdmin: false,
      isPlatformAdmin: true,
      loading: false,
      initialized: true,
      initializationError: null,
    };
  }

  if (me.role === 'platform_admin') {
    throw new Error('Tenant session is unavailable');
  }
  const { activeWorkspaceRole, capabilities } = requireTenantPermissionFields(me);
  return {
    userId: me.user_id,
    username: me.username,
    displayName: me.display_name || me.username,
    tenantId: me.tenant_id,
    role: me.role,
    activeWorkspaceRole,
    capabilities,
    mustChangePassword: Boolean(me.must_change_password),
    sessionPlane,
    workspaceIds: me.workspace_ids,
    workspaces: me.workspaces || me.workspace_ids.map((id) => ({ id, name: id })),
    activeWorkspaceId: me.active_workspace_id,
    activeWorkspaceName: me.active_workspace_name,
    isAdmin: activeWorkspaceRole === 'tenant_admin' || activeWorkspaceRole === 'ws_admin',
    isTenantAdmin: activeWorkspaceRole === 'tenant_admin',
    isPlatformAdmin: false,
    loading: false,
    initialized: true,
    initializationError: null,
  };
}

async function fetchMeForPlane(plane: SessionPlane): Promise<MeResponse | null> {
  return plane === 'platform' ? fetchPlatformCurrentUser() : fetchTenantCurrentUser();
}

export const useAuthStore = create<AuthStoreState>()(
  devtools(
    (set, get) => ({
      ...initialState,

      initialize: async () => {
        const pathname = window.location.pathname;
        const onPublicPath = isPublicPath(pathname);
        const sessionPlane = getSessionPlaneForPath(pathname);
        set(
          {
            loading: true,
            initialized: false,
            initializationError: null,
            sessionPlane,
          },
          false,
          'initialize/start',
        );

        try {
          const status = await fetchAuthStatus();
          if (!status.enabled) {
            set(
              {
                ...initialState,
                authEnabled: false,
                loading: false,
                initialized: true,
                sessionPlane,
              },
              false,
              'initialize/authDisabled',
            );
            return;
          }

          if (onPublicPath) {
            set(
              {
                authEnabled: true,
                ssoEnabled: status.sso_enabled,
                ssoLoginPath: status.sso_login_path,
                loading: false,
                initialized: true,
                sessionPlane,
                initializationError: null,
              },
              false,
              'initialize/public',
            );
            return;
          }

          if (sessionPlane === 'tenant') {
            const params = new URLSearchParams(window.location.search);
            const code = params.get('code');
            if (code) {
              const { exchangeCode } = await import('@/auth');
              await exchangeCode(code, window.location.origin + pathname);
              params.delete('code');
              const cleanUrl = pathname + (params.toString() ? `?${params}` : '');
              window.history.replaceState(null, '', cleanUrl);
            }
          }

          const me = await fetchMeForPlane(sessionPlane);
          if (!me) {
            if (sessionPlane === 'tenant' && isGraphDataPath(pathname)) {
              const platformMe = await fetchPlatformCurrentUser().catch(() => null);
              if (platformMe?.role === 'platform_admin') {
                set(
                  {
                    authEnabled: true,
                    ssoEnabled: status.sso_enabled,
                    ssoLoginPath: status.sso_login_path,
                    ...authStateFromMe(platformMe, 'platform'),
                  },
                  false,
                  'initialize/platformForbidden',
                );
                return;
              }
            }
            set(
              {
                authEnabled: true,
                ssoEnabled: status.sso_enabled,
                ssoLoginPath: status.sso_login_path,
                loading: false,
                initialized: true,
                initializationError: null,
                sessionPlane,
              },
              false,
              'initialize/anonymous',
            );
            if (status.sso_enabled && status.sso_login_path) {
              const redirect = encodeURIComponent(window.location.href);
              window.location.replace(`${status.sso_login_path}?back_url=${redirect}`);
            } else {
              window.location.replace(buildLocalLoginUrl(sessionPlane));
            }
            return;
          }
          set(
            {
              authEnabled: true,
              ssoEnabled: status.sso_enabled,
              ssoLoginPath: status.sso_login_path,
              ...authStateFromMe(me, sessionPlane),
            },
            false,
            'initialize/success',
          );
        } catch {
          set(
            {
              authEnabled: true,
              loading: false,
              initialized: true,
              sessionPlane,
              initializationError: 'AUTH_INITIALIZATION_FAILED',
            },
            false,
            'initialize/error',
          );
        }
      },

      refresh: async () => {
        const previous = get();
        const me = await fetchMeForPlane(previous.sessionPlane);
        if (!me) {
          throw new Error('Session expired');
        }
        const next = authStateFromMe(me, previous.sessionPlane);
        set(next, false, 'refresh/success');
        if (
          previous.activeWorkspaceId !== next.activeWorkspaceId ||
          previous.activeWorkspaceRole !== next.activeWorkspaceRole
        ) {
          const { clearWorkspaceScopedState } = await import('@/utils/workspaceState');
          clearWorkspaceScopedState(previous.activeWorkspaceId);
        }
      },

      logout: async () => {
        try {
          if (get().sessionPlane === 'platform') {
            await platformLogout();
          } else {
            await tenantLogout();
          }
        } finally {
          set(
            {
              ...initialState,
              loading: false,
              initialized: true,
              sessionPlane: get().sessionPlane,
            },
            false,
            'logout',
          );
        }
      },

      clear: () => {
        set(
          {
            ...initialState,
            loading: false,
            initialized: true,
          },
          false,
          'clear',
        );
      },
    }),
    { name: 'AuthStore' },
  ),
);

export const useAuthEnabled = () => useAuthStore((state) => state.authEnabled);
export const useCurrentRole = () => useAuthStore((state) => state.role);
export const useActiveWorkspaceRole = () => useAuthStore((state) => state.activeWorkspaceRole);
export const useCapabilities = () => useAuthStore((state) => state.capabilities);
export const useHasCapability = (resource: Resource) =>
  useAuthStore((state) => !state.authEnabled || state.capabilities.includes(resource));
export const useIsAdmin = () => useAuthStore((state) => state.isAdmin);
export const useIsTenantAdmin = () => useAuthStore((state) => state.isTenantAdmin);
export const useIsPlatformAdmin = () => useAuthStore((state) => state.isPlatformAdmin);
export const useAuthLoading = () => useAuthStore((state) => state.loading);
export const useAuthInitialized = () => useAuthStore((state) => state.initialized);
export const useActiveWorkspace = () =>
  useAuthStore(
    useShallow((state) => ({
      id: state.activeWorkspaceId,
      name: state.activeWorkspaceName,
    })),
  );
export const useWorkspaceIds = () => useAuthStore((state) => state.workspaceIds);
export const useWorkspaces = () => useAuthStore((state) => state.workspaces);
export const useCurrentUser = () =>
  useAuthStore(
    useShallow((state) => ({
      userId: state.userId,
      username: state.username,
      displayName: state.displayName,
    })),
  );
