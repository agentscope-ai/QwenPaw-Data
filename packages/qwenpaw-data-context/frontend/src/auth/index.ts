// Types
export type {
  Role,
  TenantRole,
  ScopeType,
  AuthState,
  Resource,
  TenantStatus,
  TenantOnboardingState,
  WorkspaceStatus,
  WorkspaceInstanceStatus,
  ProvisionBackend,
  UserStatus,
  AuthStatusResponse,
  OnboardingStatus,
  OnboardingTenant,
  ApplyOpenTenant,
  MyAccessRequest,
  AccessRequest,
  AccessRequestStatus,
  AccessRequestTargetType,
  ChooserKind,
  ChooserOption,
  ChooserStatus,
  LoginResponse,
  PlatformLoginResponse,
  WorkspaceInfo,
  Tenant,
  Workspace,
  UserRoleBinding,
  User,
  RoleBinding,
  MeResponse,
  ChangePasswordRequest,
  RawCypherGrantRequest,
  RawCypherGrantResponse,
} from './types';
export { EMPTY_AUTH_STATE, DEFAULT_PROVISION_BACKENDS, normalizeProvisionBackends } from './types';

// Permissions
export { checkPermission } from './permissions';

// Config
export {
  getAuthApiUrl,
  getAuthBaseUrl,
  getAuthFrontendUrl,
  getContextFrontendUrl,
  getHostFrontendUrl,
  resolveAgentCenterUrl,
  currentPageDatasourceId,
  isAgentCenterSameApp,
  getConfiguredRedirectOrigins,
  getConfiguredRedirectPaths,
  buildLoginUrl,
  buildAuthApiUrl,
} from './config';

// Auth API
export {
  formatDetail,
  AuthApiError,
  authFetch,
  fetchAuthStatus,
  fetchOnboardingStatus,
  fetchChooserStatus,
  selectChooserAccount,
  submitAccessRequest,
  cancelAccessRequest,
  listAccessRequests,
  approveAccessRequest,
  rejectAccessRequest,
  login,
  register,
  fetchCurrentUser,
  fetchTenantCurrentUser,
  fetchPlatformCurrentUser,
  platformBootstrap,
  platformLogin,
  platformLogout,
  switchWorkspace,
  changePassword,
  issueRawCypherGrant,
  revokeRawCypherGrant,
  logout,
  refreshToken,
  exchangeCode,
} from './auth';
export type { AuthSessionPlane, CurrentUserSession, AuthRequestOptions } from './auth';

// Components & Hooks
export { AuthGuard } from './AuthGuard';
export type { AuthGuardProps } from './AuthGuard';
export { usePermission } from './usePermission';
export { useAuth } from './useAuth';
