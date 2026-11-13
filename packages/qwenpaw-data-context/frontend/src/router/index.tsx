import React from 'react';
import Layout from '@/layout/index';
import AuthLayout from '@/layout/AuthLayout';
import PermissionGuard from '@/components/PermissionGuard';
import { getAgentCenterLanding } from '@/utils/redirectAfterAuth';
import { useAuthStore } from '@/store/authStore';
import type { Role } from '@/auth';
import { PageReady } from '../embedded/bridge';
import { Navigate, redirect, useLocation, type RouteObject } from 'react-router-dom';

export const ROUTES = {
  HOME: '/',
  DATA_SOURCE: '/data-source',
  LOGIN: '/login',
  PLATFORM_LOGIN: '/platform/login',
  REGISTER: '/register',
  ONBOARDING: '/onboarding',
  CHOOSE_ACCOUNT: '/choose-account',
  DATA_SET: '/data-set',
  COLUMN: '/column',
  DIMENSION: '/dimension',
  DIMENSION_VALUE: '/dimension-value',
  DIMENSION_CALIBER: '/dimension-caliber',
  METRIC_LIB: '/metric-lib',
  METRIC_ADD: '/metric-lib/add',
  METRIC_FORMULA_LIB: '/metric-formula-lib',
  EXCEL_IMPORT: '/excel-import',
  SEMANTIC_WEAVING: '/semantic-weaving',
  MODEL_CONFIG: '/model-config',
  DATA_CONNECTION: '/data-connection',
  DATA_CONNECTION_ADD: '/data-connection/add',
  DATA_PROFILING: '/data-profiling',
  DATA_PROFILING_DETAIL: '/data-profiling/:datasourceId',
  CM_GRAPH: '/cm-graph',
  GRAPH_CENTER: '/graph-center',
  GRAPH_MANAGEMENT: '/graph-management',
  GRAPH_MANAGEMENT_WORKSPACE: '/graph-management/workspace/:workspaceId',
  GRAPH_MANAGEMENT_EXPLORE: '/graph-management/explore',
  GRAPH_MANAGEMENT_RAW_CYPHER: '/graph-management/raw-cypher',
  KG_DOCS: '/kg-docs',
  BUSINESS_DOMAIN: '/business-domain',
  UC_CONSOLE: '/user-center/console',
  UC_DASHBOARD: '/user-center/dashboard',
  UC_MEMBERS: '/user-center/members',
  UC_WORKSPACES: '/user-center/workspaces',
  UC_TENANTS: '/user-center/tenants',
  UC_RAW_CYPHER_GRANTS: '/user-center/raw-cypher-grants',
  UC_HOME: '/user-center/home',
  UC_ACCOUNT: '/user-center/account',
  UC_CHANGE_PASSWORD: '/user-center/account/change-password',
  FORBIDDEN: '/403',
  NOT_FOUND: '*',
};

export interface BreadcrumbHandle {
  titleKey: string;
  descriptionKey?: string;
  headerAction?: { labelKey: string; path: string };
  dynamicTitle?: (params: URLSearchParams) => {
    titleKey: string;
    parent?: { titleKey: string; path: string };
  } | null;
  parent?: { titleKey: string; path: string };
}

const DatasetManagementPage = React.lazy(() => import('@/pages/DatasetManagement'));
const ColumnManagementPage = React.lazy(() => import('@/pages/ColumnManagement'));
const DimensionManagementPage = React.lazy(() => import('@/pages/DimensionManagement'));
const DimensionValueManagementPage = React.lazy(() => import('@/pages/DimensionValueManagement'));
const DimensionCaliberManagementPage = React.lazy(
  () => import('@/pages/DimensionCaliberManagement'),
);
const MetricLibPage = React.lazy(() => import('@/pages/MetricLib'));
const MetricAddPage = React.lazy(() => import('@/pages/MetricLib/MetricLibAdd'));
const MetricFormulaLibPage = React.lazy(() => import('@/pages/MetricFormulaLib'));
const SemanticWeavingPage = React.lazy(() => import('@/pages/SemanticWeaving'));
const ModelConfigPage = React.lazy(() => import('@/pages/ModelConfigRedirect'));
const DataConnectionPage = React.lazy(() => import('@/pages/DataConnection'));
const DataConnectionAddPage = React.lazy(() => import('@/pages/DataConnection/Add'));
const DataProfilingPage = React.lazy(() => import('@/pages/DataProfiling'));
const DataProfilingWorkbenchPage = React.lazy(() => import('@/pages/DataProfiling/Workbench'));
const CMGraphPage = React.lazy(() => import('@/pages/CMGraph'));
const GraphCenterPage = React.lazy(() => import('@/pages/GraphCenter'));
const GraphManagementPage = React.lazy(() => import('@/pages/GraphManagement'));
const WorkspaceDetailPage = React.lazy(() => import('@/pages/GraphManagement/WorkspaceDetail'));
const KGDocsPage = React.lazy(() => import('@/pages/KGDocs'));
const DataSourceManagementPage = React.lazy(() => import('@/pages/DataSourceManagement'));
const BusinessDomainManagementPage = React.lazy(() => import('@/pages/BusinessDomainManagement'));
const NotFoundPage = React.lazy(() => import('@/pages/NotFoundPage'));
const RouteErrorBoundary = React.lazy(() => import('@/pages/RouteErrorBoundary'));
const NoPermissionPage = React.lazy(() => import('@/pages/NoPermissionPage'));
const LoginPage = React.lazy(() => import('@/pages/Auth/Login'));
const PlatformLoginPage = React.lazy(() => import('@/pages/Auth/PlatformLogin'));
const RegisterPage = React.lazy(() => import('@/pages/Auth/Register'));
const UserCenterConsolePage = React.lazy(() => import('@/pages/UserCenter/Console'));
const UserCenterTenantsPage = React.lazy(() => import('@/pages/UserCenter/TenantManagement'));
const UserCenterRawCypherGrantsPage = React.lazy(
  () => import('@/pages/UserCenter/RawCypherGrants'),
);
const UserCenterAccountPage = React.lazy(() => import('@/pages/UserCenter/Account'));
const UserCenterChangePasswordPage = React.lazy(() => import('@/pages/UserCenter/ChangePassword'));
const RawCypherPage = React.lazy(() => import('@/pages/RawCypher'));
const guard = (
  resource: React.ComponentProps<typeof PermissionGuard>['resource'],
  element: React.ReactNode,
) => (
  <PermissionGuard resource={resource}>
    <PageReady>{element}</PageReady>
  </PermissionGuard>
);

/** Role-based guard for User Center pages (permission matrix does not cover them). */
function RoleGuard({ roles, children }: { roles: Role[]; children: React.ReactNode }) {
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const role = useAuthStore((s) => s.role);
  const activeWorkspaceRole = useAuthStore((s) => s.activeWorkspaceRole);
  const effectiveRole: Role = role === 'platform_admin' ? role : activeWorkspaceRole;
  if (authEnabled && !roles.includes(effectiveRole)) {
    return (
      <React.Suspense fallback={null}>
        <NoPermissionPage />
      </React.Suspense>
    );
  }
  return <>{children}</>;
}

const roleGuard = (roles: Role[], element: React.ReactNode) => (
  <RoleGuard roles={roles}>{element}</RoleGuard>
);

function PrivateSessionGate({ children }: { children: React.ReactNode }) {
  const authEnabled = useAuthStore((state) => state.authEnabled);
  const userId = useAuthStore((state) => state.userId);
  const sessionPlane = useAuthStore((state) => state.sessionPlane);
  const mustChangePassword = useAuthStore((state) => state.mustChangePassword);
  const location = useLocation();

  if (
    authEnabled &&
    userId &&
    sessionPlane === 'tenant' &&
    mustChangePassword &&
    location.pathname !== ROUTES.UC_CHANGE_PASSWORD
  ) {
    const redirectTarget = `${location.pathname}${location.search}${location.hash}`;
    return (
      <Navigate
        replace
        to={`${ROUTES.UC_CHANGE_PASSWORD}?redirect=${encodeURIComponent(redirectTarget)}`}
      />
    );
  }

  return <>{children}</>;
}

/** Loader factory for legacy auth-console URLs, keeps the query string. */
const legacyRedirect =
  (to: string, search?: string) =>
  ({ request }: { request: Request }) => {
    const url = new URL(request.url);
    return redirect(`${to}${search ?? url.search}`);
  };

/**
 * Full-page jump to the Host console — a separate SPA React Router cannot reach.
 * Must stay an element, not a homeLoader side effect: loaders run before any
 * element renders and would outrun PrivateSessionGate's forced password change.
 */
function AgentCenterRedirect() {
  React.useEffect(() => {
    const target = getAgentCenterLanding();
    if (target) window.location.replace(target);
  }, []);
  return null;
}

/** Platform admins go to tenant management; everyone else lands on the Host console. */
async function homeLoader() {
  const { authEnabled, isPlatformAdmin } = useAuthStore.getState();
  if (authEnabled && isPlatformAdmin) {
    return redirect(ROUTES.UC_TENANTS);
  }
  if (!getAgentCenterLanding()) {
    return redirect(ROUTES.DATA_SOURCE);
  }
  return null;
}

export function createContextRoutes(): RouteObject[] {
  return [
  {
    element: <AuthLayout />,
    children: [
      { path: ROUTES.LOGIN, element: <LoginPage /> },
      { path: ROUTES.ONBOARDING, element: <Navigate to={ROUTES.LOGIN} replace /> },
      { path: ROUTES.CHOOSE_ACCOUNT, element: <Navigate to={ROUTES.LOGIN} replace /> },
      { path: ROUTES.PLATFORM_LOGIN, element: <PlatformLoginPage /> },
      { path: ROUTES.REGISTER, element: <RegisterPage /> },
    ],
  },
  /* Legacy auth-console URLs — keep old links and bookmarks working. */
  { path: '/admin', loader: legacyRedirect(ROUTES.UC_DASHBOARD, '') },
  { path: '/admin/dashboard', loader: legacyRedirect(ROUTES.UC_DASHBOARD) },
  { path: '/admin/members', loader: legacyRedirect(ROUTES.UC_MEMBERS) },
  { path: '/admin/workspaces', loader: legacyRedirect(ROUTES.UC_WORKSPACES) },
  { path: '/admin/users', loader: legacyRedirect(ROUTES.UC_MEMBERS, '?tab=members') },
  { path: '/admin/roles', loader: legacyRedirect(ROUTES.UC_MEMBERS, '?tab=members') },
  { path: '/platform/dashboard', loader: legacyRedirect(ROUTES.UC_DASHBOARD) },
  { path: '/platform/tenants', loader: legacyRedirect(ROUTES.UC_TENANTS) },
  { path: '/home', loader: legacyRedirect(ROUTES.UC_HOME) },
  { path: '/account', loader: legacyRedirect(ROUTES.UC_ACCOUNT) },
  { path: '/account/change-password', loader: legacyRedirect(ROUTES.UC_CHANGE_PASSWORD) },
  {
    path: '/',
    element: (
      <PrivateSessionGate>
        <Layout />
      </PrivateSessionGate>
    ),
    errorElement: <RouteErrorBoundary />,
    children: [
      {
        path: ROUTES.HOME,
        loader: homeLoader,
        element: <AgentCenterRedirect />,
      },
      {
        path: ROUTES.DATA_SOURCE,
        element: guard('datasource:read', <DataSourceManagementPage />),
        handle: { titleKey: 'routes.dataSourceManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_SET,
        element: guard('datasource:read', <DatasetManagementPage />),
        handle: { titleKey: 'routes.datasetManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.COLUMN,
        element: guard('datasource:read', <ColumnManagementPage />),
        handle: {
          titleKey: 'routes.columnManagement',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetId = params.get('dataset_id');
            if (datasetId) {
              if (mode === 'generate') {
                return {
                  titleKey: 'routes.generateColumn',
                  parent: { titleKey: 'routes.datasetManagement', path: ROUTES.HOME },
                };
              }
              return {
                titleKey: 'routes.columnManagement',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.HOME },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION,
        element: guard('datasource:read', <DimensionManagementPage />),
        handle: {
          titleKey: 'routes.dimensionManagement',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetIds = params.get('datasetIds');
            if (mode === 'preview' && datasetIds) {
              return {
                titleKey: 'routes.generateDimension',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.HOME },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION_VALUE,
        element: guard('datasource:read', <DimensionValueManagementPage />),
        handle: {
          titleKey: 'routes.dimensionValue',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetIds = params.get('datasetIds');
            if (mode === 'preview' && datasetIds) {
              return {
                titleKey: 'routes.fetchDimensionValue',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.HOME },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION_CALIBER,
        element: guard('datasource:read', <DimensionCaliberManagementPage />),
        handle: { titleKey: 'routes.dimensionCaliber' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_LIB,
        element: guard('datasource:read', <MetricLibPage />),
        handle: { titleKey: 'routes.metricLib' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_ADD,
        element: guard('datasource:write', <MetricAddPage />),
        handle: {
          titleKey: 'routes.addMetric',
          parent: { titleKey: 'routes.metricLib', path: ROUTES.METRIC_LIB },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_FORMULA_LIB,
        element: guard('datasource:read', <MetricFormulaLibPage />),
        handle: {
          titleKey: 'routes.metricFormula',
          dynamicTitle: (params: URLSearchParams) => {
            const prefill = params.get('prefill');
            if (prefill === 'true') {
              return {
                titleKey: 'routes.editMetricFormula',
                parent: { titleKey: 'routes.metricLib', path: ROUTES.METRIC_LIB },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.EXCEL_IMPORT,
        loader: () => redirect(ROUTES.DATA_SOURCE),
      },
      {
        path: ROUTES.SEMANTIC_WEAVING,
        element: guard('semantic_weaving', <SemanticWeavingPage />),
        handle: { titleKey: 'routes.semanticWeaving' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.MODEL_CONFIG,
        element: guard('model_config', <ModelConfigPage />),
        handle: { titleKey: 'routes.modelConfig' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_CONNECTION,
        element: guard('datasource:read', <DataConnectionPage />),
        handle: { titleKey: 'routes.dataConnection' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_CONNECTION_ADD,
        element: guard('datasource:write', <DataConnectionAddPage />),
        handle: {
          titleKey: 'routes.addDataSource',
          parent: { titleKey: 'routes.dataConnection', path: ROUTES.DATA_CONNECTION },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_PROFILING,
        element: guard('datasource:read', <DataProfilingPage />),
        handle: {
          titleKey: 'routes.dataProfiling',
          descriptionKey: 'dataProfiling.overviewDescription',
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_PROFILING_DETAIL,
        element: guard('datasource:read', <DataProfilingWorkbenchPage />),
        handle: {
          titleKey: 'routes.dataProfilingWorkbench',
          headerAction: {
            labelKey: 'dataProfiling.backToOverview',
            path: ROUTES.DATA_PROFILING,
          },
          parent: { titleKey: 'routes.dataProfiling', path: ROUTES.DATA_PROFILING },
        } as BreadcrumbHandle,
      },
      {
        // Legacy CM Graph URL — keep old links working.
        path: ROUTES.CM_GRAPH,
        loader: () => redirect(ROUTES.GRAPH_MANAGEMENT),
      },
      {
        // Graph Center (图谱中心): dashboard / search / entity detail / global graph.
        // Nested <Routes> inside the shell handle the sub-paths.
        path: `${ROUTES.GRAPH_CENTER}/*`,
        element: guard('mg:read', <GraphCenterPage />),
        handle: { titleKey: 'routes.graphCenter' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.GRAPH_MANAGEMENT,
        element: guard('mg:read', <GraphManagementPage />),
        handle: { titleKey: 'routes.graphManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.GRAPH_MANAGEMENT_WORKSPACE,
        element: guard('mg:read', <WorkspaceDetailPage />),
        handle: { titleKey: 'routes.graphManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.GRAPH_MANAGEMENT_EXPLORE,
        element: guard('mg:read', <CMGraphPage />),
        handle: { titleKey: 'routes.graphManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.GRAPH_MANAGEMENT_RAW_CYPHER,
        element: guard('raw_cypher', <RawCypherPage />),
        handle: { titleKey: 'routes.rawCypher' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.KG_DOCS,
        element: guard('tg_kg:self', <KGDocsPage />),
        handle: { titleKey: 'routes.kgDocs' } as BreadcrumbHandle,
      },

      {
        path: ROUTES.BUSINESS_DOMAIN,
        element: guard('datasource:read', <BusinessDomainManagementPage />),
        handle: { titleKey: 'routes.businessDomainManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.UC_CONSOLE,
        element: roleGuard(
          ['tenant_admin', 'ws_admin', 'member', 'platform_admin'],
          <UserCenterConsolePage />,
        ),
        handle: { titleKey: 'routes.userCenterConsole' } as BreadcrumbHandle,
      },
      /* Old user-center URLs — redirect into the merged console, mapping tabs. */
      {
        path: ROUTES.UC_DASHBOARD,
        loader: () => redirect(ROUTES.UC_CONSOLE),
      },
      {
        path: ROUTES.UC_MEMBERS,
        loader: () => redirect(`${ROUTES.UC_CONSOLE}?tab=members`),
      },
      {
        path: ROUTES.UC_WORKSPACES,
        loader: () => redirect(`${ROUTES.UC_CONSOLE}?tab=workspaces`),
      },
      {
        path: ROUTES.UC_TENANTS,
        element: roleGuard(['platform_admin'], <UserCenterTenantsPage />),
        handle: { titleKey: 'routes.userCenterTenants' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.UC_RAW_CYPHER_GRANTS,
        element: roleGuard(['platform_admin'], <UserCenterRawCypherGrantsPage />),
        handle: { titleKey: 'routes.rawCypherGrants' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.UC_HOME,
        loader: () => redirect(ROUTES.UC_CONSOLE),
      },
      {
        path: ROUTES.UC_ACCOUNT,
        element: roleGuard(['member', 'ws_admin', 'tenant_admin'], <UserCenterAccountPage />),
        handle: { titleKey: 'routes.userCenterAccount' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.UC_CHANGE_PASSWORD,
        element: roleGuard(
          ['member', 'ws_admin', 'tenant_admin'],
          <UserCenterChangePasswordPage />,
        ),
        handle: {
          titleKey: 'routes.userCenterChangePassword',
          parent: { titleKey: 'routes.userCenterAccount', path: ROUTES.UC_ACCOUNT },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.FORBIDDEN,
        element: <NoPermissionPage />,
        handle: { titleKey: 'routes.noPermission' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.NOT_FOUND,
        element: <NotFoundPage />,
        handle: { titleKey: 'routes.notFound' } as BreadcrumbHandle,
      },
    ],
  },
  ];
}
