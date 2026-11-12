export type Role = 'platform_admin' | 'tenant_admin' | 'ws_admin' | 'member';
export type TenantRole = Exclude<Role, 'platform_admin'>;
export type ScopeType = 'tenant' | 'workspace';

export type TenantStatus = 'provisioning' | 'active' | 'suspended';
export type TenantOnboardingState = 'pending' | 'ready';
export type WorkspaceStatus = 'provisioning' | 'active' | 'suspended' | 'archived';
export type WorkspaceInstanceStatus = 'pending' | 'queued' | 'provisioning' | 'ready' | 'failed';
export type ProvisionBackend = 'neo4j' | 'holo_age';

export const DEFAULT_PROVISION_BACKENDS: ProvisionBackend[] = ['neo4j'];

export function normalizeProvisionBackends(raw?: readonly string[] | null): ProvisionBackend[] {
  const allowed: ProvisionBackend[] = ['neo4j', 'holo_age'];
  const list = (raw || []).filter((item): item is ProvisionBackend =>
    allowed.includes(item as ProvisionBackend),
  );
  return list.length > 0 ? list : DEFAULT_PROVISION_BACKENDS;
}
export type UserStatus = 'pending' | 'active' | 'disabled';

export type Resource =
  | 'mg:read'
  | 'mg:write'
  | 'mg_copy:self'
  | 'mg_userspace:sync'
  | 'mg_userspace:append'
  | 'tg_kg:self'
  | 'tg_kg:others_read'
  | 'workspace:manage'
  | 'user:manage'
  | 'datasource:read'
  | 'datasource:write'
  | 'skill:publish'
  | 'skill:install'
  | 'snapshot:create'
  | 'snapshot:rollback_mg'
  | 'snapshot:rollback_self'
  | 'raw_cypher'
  | 'semantic_weaving'
  | 'excel_import'
  | 'model_config'
  | 'ops:logs';

export interface AuthState {
  isAuthenticated: boolean;
  userId: string;
  username: string;
  // Preferred display name; falls back to username when absent.
  displayName: string;
  tenantId: string;
  role: Role;
  workspaceIds: string[];
  activeWorkspaceId: string;
  activeWorkspaceName: string;
  isAdmin: boolean;
  isTenantAdmin: boolean;
  isPlatformAdmin: boolean;
  activeWorkspaceRole?: TenantRole;
  capabilities?: Resource[];
  mustChangePassword?: boolean;
}

export const EMPTY_AUTH_STATE: AuthState = {
  isAuthenticated: false,
  userId: '',
  username: '',
  displayName: '',
  tenantId: '',
  role: 'member',
  workspaceIds: [],
  activeWorkspaceId: '',
  activeWorkspaceName: '',
  isAdmin: false,
  isTenantAdmin: false,
  isPlatformAdmin: false,
  activeWorkspaceRole: 'member',
  capabilities: [],
  mustChangePassword: false,
};

export interface AuthStatusResponse {
  enabled: boolean;
  has_users: boolean;
  has_platform_admin: boolean;
  platform_bootstrap_required: boolean;
  legacy_registration_enabled: boolean;
  sso_enabled: boolean;
  sso_login_path: string;
}

export interface OnboardingTenant {
  id: string;
  name: string;
}

export type AccessRequestTargetType = 'tenant' | 'workspace';
export type AccessRequestStatus = 'pending' | 'approved' | 'rejected' | 'canceled';

/** A tenant open to join requests. `tenant_open` is false for a tenant
 * reachable only through one of its individually opened workspaces. */
export interface ApplyOpenTenant extends OnboardingTenant {
  tenant_open: boolean;
  workspaces: OnboardingTenant[];
}

export interface MyAccessRequest {
  id: string;
  target_type: AccessRequestTargetType;
  target_id: string;
  target_name: string;
  requested_role: 'member';
  message: string;
  status: AccessRequestStatus;
  review_comment: string | null;
  created_at: string | null;
  reviewed_at: string | null;
}

export interface AccessRequest extends MyAccessRequest {
  emp_id: string;
  name: string;
}

export interface OnboardingStatus {
  emp_id: string;
  name: string;
  apply_open_tenants: ApplyOpenTenant[];
  my_requests: MyAccessRequest[];
}

export type ChooserKind = 'platform' | 'tenant';

export interface ChooserOption {
  id: string;
  kind: ChooserKind;
  role: Role;
  tenant_id: string;
  tenant_name: string;
  workspace_id: string;
  workspace_name: string;
}

export interface ChooserStatus {
  emp_id: string;
  name: string;
  options: ChooserOption[];
}

export interface LoginResponse {
  must_change_password?: boolean;
  tenant_id: string;
  role: Role;
  workspaces: Array<{ id: string; name: string }>;
  active_workspace?: { id: string; name: string };
}

export interface PlatformLoginResponse {
  role: 'platform_admin';
}

export interface WorkspaceInfo {
  id: string;
  name: string;
}

export interface Tenant {
  id: string;
  name: string;
  description?: string;
  neo4j_database: string;
  status: TenantStatus;
  onboarding_state: TenantOnboardingState;
  onboarding_error: string | null;
  // Integer in the control plane; NULL is treated as closed.
  join_open?: number | null;
  instance_status?: WorkspaceInstanceStatus | null;
  instance_version?: string | null;
  endpoint_ref?: string | null;
  created_at: string | null;
}

export interface Workspace {
  id: string;
  tenant_id: string;
  name: string;
  description: string;
  status: WorkspaceStatus;
  endpoint_ref: string | null;
  secret_ref: string | null;
  join_open?: number | null;
  instance_status: WorkspaceInstanceStatus;
  instance_version: string | null;
  member_count?: number;
  can_manage: boolean;
  created_at: string | null;
}

export interface UserRoleBinding {
  id: string;
  role: TenantRole;
  scope_type: ScopeType;
  scope_id: string;
  scope_name: string;
  can_delete?: boolean;
  deny_reason?: string | null;
}

export interface User {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  status: UserStatus;
  created_at: string | null;
  role_bindings: UserRoleBinding[];
  can_disable?: boolean;
  can_delete?: boolean;
  deny_reason?: string | null;
}

export interface RoleBinding extends UserRoleBinding {
  user_id: string;
  username?: string;
  created_at: string | null;
}

export interface MeResponse {
  user_id: string;
  username: string;
  display_name?: string;
  tenant_id: string;
  role: Role;
  workspace_ids: string[];
  workspaces: WorkspaceInfo[];
  active_workspace_id: string;
  active_workspace_name: string;
  active_workspace_role?: TenantRole;
  capabilities: Resource[];
  must_change_password: boolean;
}

export interface ChangePasswordRequest {
  current_password?: string;
  new_password: string;
}

export interface RawCypherGrantRequest {
  actor_user_id: string;
  tenant_id: string;
  workspace_id: string;
  cypher: string;
  params: Record<string, unknown>;
  ttl_seconds: number;
}

export interface RawCypherGrantResponse {
  grant_id: string;
  grant_token: string;
  expires_at: string;
}
