declare module '@qwenpaw-data/cm' {
  export function createContextRoutes(): import('react-router-dom').RouteObject[];
  export function configureEmbedding(): void;
  export function configureUiBridge(
    bridge: import('../features/console/ui-runtime/registry').UiRegistry,
  ): void;
  export function initializeContextAuth(): Promise<void>;
  export function refreshContextAuth(): Promise<'unauthorized' | undefined>;
  export function authPlane(path: string): 'public' | 'tenant' | 'platform';
  export function ContextProvider(props: {
    children: import('react').ReactNode;
    language: string;
  }): import('react').ReactNode;
  export function ContextMenu(props: { language: string }): import('react').ReactNode;
  export interface ContextAuth {
    authEnabled: boolean; // Authoritative auth configuration.
    initialized: boolean; // Initialization finished.
    loading: boolean; // Identity request is pending.
    initializationError: string | null; // Initialization failure, if any.
    userId: string; // Current user identifier.
    username: string; // Login name.
    displayName: string; // Display label.
    tenantId: string; // Current tenant identifier.
    activeWorkspaceId: string; // Current workspace identifier.
    activeWorkspaceName: string; // Workspace display label.
    workspaces: { id: string; name: string }[]; // Accessible workspace options.
    workspaceIds: string[]; // Accessible workspace identifiers.
    role: 'member' | 'ws_admin' | 'tenant_admin' | 'platform_admin'; // Authoritative account role.
    activeWorkspaceRole: 'member' | 'ws_admin' | 'tenant_admin'; // Authoritative workspace role.
    capabilities: string[]; // Server-provided permission list.
    mustChangePassword: boolean; // Blocks normal tenant pages until password update.
    sessionPlane: 'tenant' | 'platform'; // Identity domain used by the current route.
  }
  export function getContextAuth(): ContextAuth;
  export function subscribeContextAuth(listener: (state: ContextAuth) => void): () => void;
}
