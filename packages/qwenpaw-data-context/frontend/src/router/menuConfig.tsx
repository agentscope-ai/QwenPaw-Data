import {
  ApartmentOutlined,
  AppstoreOutlined,
  AreaChartOutlined,
  BankOutlined,
  BranchesOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  FundOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@/design';
import type { ReactNode } from 'react';
import type { Resource, Role } from '@/auth';

interface MenuItem {
  nameKey: string;
  /** 叶子/子组的路由路径；顶层分组不设 path，仅做展开/折叠。 */
  path?: string;
  key?: string;
  icon?: ReactNode;
  children?: MenuItem[];
  requiredPermission?: Resource;
  /** Role-gated entries (User Center); hidden entirely when auth is disabled. */
  requiredRoles?: Role[];
  memberReadOnly?: boolean;
}

const asideMenuConfig: MenuItem[] = [
  {
    nameKey: 'menu.dataModeling',
    key: 'data-modeling-group',
    children: [
      {
        nameKey: 'menu.dataSourceManagement',
        path: '/data-source',
        key: 'data-source-management',
        icon: <DatabaseOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
      },
      {
        nameKey: 'menu.dataProfiling',
        path: '/data-profiling',
        key: 'data-profiling',
        icon: <ExperimentOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
      },
      {
        nameKey: 'menu.businessDomainManagement',
        path: '/business-domain',
        key: 'business-domain',
        icon: <AppstoreOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
      },
      {
        nameKey: 'menu.dataset',
        path: '/data-set',
        key: 'dataset-group',
        icon: <AreaChartOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
        children: [
          {
            nameKey: 'menu.datasetManagement',
            path: '/data-set',
            key: 'dataset-management',
          },
          {
            nameKey: 'menu.columnManagement',
            path: '/column',
            key: 'column-management',
          },
        ],
      },
      {
        nameKey: 'menu.dimension',
        path: '/dimension',
        key: 'dimension-group',
        icon: <ApartmentOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
        children: [
          {
            nameKey: 'menu.dimensionManagement',
            path: '/dimension',
            key: 'dimension-management',
          },
          {
            nameKey: 'menu.dimensionCaliber',
            path: '/dimension-caliber',
            key: 'dimension-caliber',
          },
          // {
          //   nameKey: 'menu.dimensionValue',
          //   path: '/dimension-value',
          // },
        ],
      },
      {
        nameKey: 'menu.metric',
        path: '/metric-lib',
        key: 'metric-group',
        icon: <FundOutlined />,
        requiredPermission: 'datasource:read',
        memberReadOnly: true,
        children: [
          {
            nameKey: 'menu.metricManagement',
            path: '/metric-lib',
            key: 'metric-lib',
          },
          {
            nameKey: 'menu.metricFormula',
            path: '/metric-formula-lib',
            key: 'metric-formula-lib',
          },
        ],
      },
      {
        nameKey: 'menu.semanticWeaving',
        path: '/semantic-weaving',
        key: 'semantic-weaving',
        icon: <BranchesOutlined />,
        requiredPermission: 'semantic_weaving',
      },
    ],
  },
  {
    nameKey: 'menu.graphCenter',
    key: 'graph-center-group',
    children: [
      {
        nameKey: 'menu.graphCenterPortal',
        path: '/graph-center/global',
        key: 'graph-center',
        icon: <DeploymentUnitOutlined />,
        requiredPermission: 'mg:read',
      },
      {
        nameKey: 'menu.kgDocs',
        path: '/kg-docs',
        key: 'kg-docs',
        icon: <FileTextOutlined />,
        requiredPermission: 'tg_kg:self',
      },
    ],
  },
  {
    nameKey: 'menu.usersAccess',
    key: 'user-center-group',
    requiredRoles: ['platform_admin', 'tenant_admin', 'ws_admin', 'member'],
    children: [
      {
        nameKey: 'menu.ucConsole',
        path: '/user-center/console',
        key: 'uc-console',
        icon: <DashboardOutlined />,
        requiredRoles: ['tenant_admin', 'ws_admin', 'member'],
      },
      {
        nameKey: 'menu.ucTenants',
        path: '/user-center/tenants',
        key: 'uc-tenants',
        icon: <BankOutlined />,
        requiredRoles: ['platform_admin'],
      },
      {
        nameKey: 'menu.rawCypherGrants',
        path: '/user-center/raw-cypher-grants',
        key: 'raw-cypher-grants',
        icon: <SafetyCertificateOutlined />,
        requiredRoles: ['platform_admin'],
      },
      {
        nameKey: 'menu.ucAccount',
        path: '/user-center/account',
        key: 'uc-account',
        icon: <UserOutlined />,
        requiredRoles: ['tenant_admin', 'ws_admin', 'member'],
      },
    ],
  },
];

/** 从菜单配置中递归提取所有叶子路径和 key（用于菜单高亮匹配） */
function extractMenuPathKeys(config: MenuItem[]): Record<string, string> {
  const pathKeys: Record<string, string> = {};
  const walk = (items: MenuItem[]) => {
    for (const item of items) {
      if (item.children?.length) {
        walk(item.children);
      } else if (item.path) {
        pathKeys[item.path] = item.key || item.path;
      }
    }
  };
  walk(config);
  return pathKeys;
}

export const menuPathKeys = extractMenuPathKeys(asideMenuConfig);

/** 所有菜单叶子路径，供 Layout 精确匹配 selectedKeys 使用 */
export const menuPaths = Object.keys(menuPathKeys);

/** 顶层分组 key（侧边栏默认展开到二级） */
export const topGroupKeys = asideMenuConfig
  .filter((item) => item.children?.length && item.key)
  .map((item) => item.key as string);

/** 菜单 key → 祖先分组 key 链，供受控 openKeys 展开当前项所在分组 */
function extractKeyAncestors(config: MenuItem[]): Record<string, string[]> {
  const ancestorsMap: Record<string, string[]> = {};
  const walk = (items: MenuItem[], ancestors: string[]) => {
    for (const item of items) {
      const key = item.key || item.path;
      if (!key) continue;
      if (ancestors.length) {
        ancestorsMap[key] = ancestors;
      }
      if (item.children?.length) {
        walk(item.children, [...ancestors, key]);
      }
    }
  };
  walk(config, []);
  return ancestorsMap;
}

export const menuKeyAncestors = extractKeyAncestors(asideMenuConfig);

export { asideMenuConfig };
