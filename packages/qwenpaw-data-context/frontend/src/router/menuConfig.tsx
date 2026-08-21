import { AreaChartOutlined, ApartmentOutlined, FundOutlined, BranchesOutlined, AppstoreOutlined, DatabaseOutlined, FileExcelOutlined, NodeIndexOutlined } from '@/design';
import type { ReactNode } from 'react';

interface MenuItem {
  nameKey: string;
  path: string;
  key?: string;
  icon?: ReactNode;
  children?: MenuItem[];
}

const asideMenuConfig: MenuItem[] = [
  {
    nameKey: 'menu.dataSourceManagement',
    path: '/data-source',
    key: 'data-source-management',
    icon: <DatabaseOutlined />,
  },
  {
    nameKey: 'menu.businessDomainManagement',
    path: '/business-domain',
    key: 'business-domain',
    icon: <AppstoreOutlined />,
  },
  {
    nameKey: 'menu.dataset',
    path: '/data-set',
    key: 'dataset-group',
    icon: <AreaChartOutlined />,
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
    nameKey: 'menu.metricManagement',
    path: '/metric-lib',
    key: 'metric-group',
    icon: <FundOutlined />,
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
  },
  {
    nameKey: 'menu.graphMemory',
    path: '/cm-graph',
    key: 'graph-memory-group',
    icon: <NodeIndexOutlined />,
    children: [
      {
        nameKey: 'menu.cmGraph',
        path: '/cm-graph',
        key: 'cm-graph',
      },
      {
        nameKey: 'menu.kgDocs',
        path: '/kg-docs',
        key: 'kg-docs',
      },
    ],
  },
  {
    nameKey: 'menu.excelImport',
    path: '/excel-import',
    key: 'excel-import',
    icon: <FileExcelOutlined />,
  },
];

/** 从菜单配置中提取所有叶子路径和 key（用于菜单高亮匹配） */
function extractMenuPathKeys(config: MenuItem[]): Record<string, string> {
  const pathKeys: Record<string, string> = {};
  for (const item of config) {
    if (item.children?.length) {
      for (const child of item.children) {
        if (child.path) {
          pathKeys[child.path] = child.key || child.path;
        }
      }
    } else if (item.path) {
      pathKeys[item.path] = item.key || item.path;
    }
  }
  return pathKeys;
}

export const menuPathKeys = extractMenuPathKeys(asideMenuConfig);

/** 所有菜单叶子路径，供 Layout 精确匹配 selectedKeys 使用 */
export const menuPaths = Object.keys(menuPathKeys);

export { asideMenuConfig };
