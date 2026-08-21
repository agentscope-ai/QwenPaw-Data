import React from 'react';
import Layout from '@/layout/index';
import {
  hasConfiguredModelApiKey,
  modelConfigApi,
} from '@/services/modelConfig';
import { createBrowserRouter, redirect } from 'react-router';

export const ROUTES = {
  HOME: '/',
  DATA_SOURCE: '/data-source',
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
  CM_GRAPH: '/cm-graph',
  KG_DOCS: '/kg-docs',
  BUSINESS_DOMAIN: '/business-domain',
  NOT_FOUND: '*',
};

export interface BreadcrumbHandle {
  titleKey: string;
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
const DimensionCaliberManagementPage = React.lazy(() => import('@/pages/DimensionCaliberManagement'));
const MetricLibPage = React.lazy(() => import('@/pages/MetricLib'));
const MetricAddPage = React.lazy(() => import('@/pages/MetricLib/MetricLibAdd'));
const MetricFormulaLibPage = React.lazy(() => import('@/pages/MetricFormulaLib'));
const ExcelImportPage = React.lazy(() => import('@/pages/ExcelImport'));
const SemanticWeavingPage = React.lazy(() => import('@/pages/SemanticWeaving'));
const ModelConfigPage = React.lazy(() => import('@/pages/ModelConfig'));
const DataConnectionPage = React.lazy(() => import('@/pages/DataConnection'));
const DataConnectionAddPage = React.lazy(() => import('@/pages/DataConnection/Add'));
const CMGraphPage = React.lazy(() => import('@/pages/CMGraph'));
const KGDocsPage = React.lazy(() => import('@/pages/KGDocs'));
const DataSourceManagementPage = React.lazy(() => import('@/pages/DataSourceManagement'));
const BusinessDomainManagementPage = React.lazy(() => import('@/pages/BusinessDomainManagement'));
const NotFoundPage = React.lazy(() => import('@/pages/NotFoundPage'));
const RouteErrorBoundary = React.lazy(() => import('@/pages/RouteErrorBoundary'));

async function redirectFromHomeByModelStatus() {
  try {
    const config = await modelConfigApi.get();
    return redirect(
      hasConfiguredModelApiKey(config)
        ? ROUTES.DATA_SOURCE
        : ROUTES.MODEL_CONFIG,
    );
  } catch {
    return redirect(ROUTES.MODEL_CONFIG);
  }
}

const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    errorElement: <RouteErrorBoundary />,
    children: [
      {
        path: ROUTES.HOME,
        loader: redirectFromHomeByModelStatus,
      },
      {
        path: ROUTES.DATA_SOURCE,
        element: <DataSourceManagementPage />,
        handle: { titleKey: 'routes.dataSourceManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_SET,
        element: <DatasetManagementPage />,
        handle: { titleKey: 'routes.datasetManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.COLUMN,
        element: <ColumnManagementPage />,
        handle: {
          titleKey: 'routes.columnManagement',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetId = params.get('datasetId');
            if (datasetId) {
              if (mode === 'generate') {
                return {
                  titleKey: 'routes.generateColumn',
                  parent: { titleKey: 'routes.datasetManagement', path: ROUTES.DATA_SET },
                };
              }
              return {
                titleKey: 'routes.columnManagement',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.DATA_SET },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION,
        element: <DimensionManagementPage />,
        handle: {
          titleKey: 'routes.dimensionManagement',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetIds = params.get('datasetIds');
            if (mode === 'preview' && datasetIds) {
              return {
                titleKey: 'routes.generateDimension',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.DATA_SET },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION_VALUE,
        element: <DimensionValueManagementPage />,
        handle: {
          titleKey: 'routes.dimensionValue',
          dynamicTitle: (params: URLSearchParams) => {
            const mode = params.get('mode');
            const datasetIds = params.get('datasetIds');
            if (mode === 'preview' && datasetIds) {
              return {
                titleKey: 'routes.fetchDimensionValue',
                parent: { titleKey: 'routes.datasetManagement', path: ROUTES.DATA_SET },
              };
            }
            return null;
          },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DIMENSION_CALIBER,
        element: <DimensionCaliberManagementPage />,
        handle: { titleKey: 'routes.dimensionCaliber' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_LIB,
        element: <MetricLibPage />,
        handle: { titleKey: 'routes.metricLib' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_ADD,
        element: <MetricAddPage />,
        handle: {
          titleKey: 'routes.addMetric',
          parent: { titleKey: 'routes.metricLib', path: ROUTES.METRIC_LIB },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.METRIC_FORMULA_LIB,
        element: <MetricFormulaLibPage />,
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
        element: <ExcelImportPage />,
        handle: { titleKey: 'routes.excelImport' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.SEMANTIC_WEAVING,
        element: <SemanticWeavingPage />,
        handle: { titleKey: 'routes.semanticWeaving' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.MODEL_CONFIG,
        element: <ModelConfigPage />,
        handle: { titleKey: 'routes.modelConfig' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_CONNECTION,
        element: <DataConnectionPage />,
        handle: { titleKey: 'routes.dataConnection' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.DATA_CONNECTION_ADD,
        element: <DataConnectionAddPage />,
        handle: {
          titleKey: 'routes.addDataSource',
          parent: { titleKey: 'routes.dataConnection', path: ROUTES.DATA_CONNECTION },
        } as BreadcrumbHandle,
      },
      {
        path: ROUTES.CM_GRAPH,
        element: <CMGraphPage />,
        handle: { titleKey: 'routes.cmGraph' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.KG_DOCS,
        element: <KGDocsPage />,
        handle: { titleKey: 'routes.kgDocs' } as BreadcrumbHandle,
      },

      {
        path: ROUTES.BUSINESS_DOMAIN,
        element: <BusinessDomainManagementPage />,
        handle: { titleKey: 'routes.businessDomainManagement' } as BreadcrumbHandle,
      },
      {
        path: ROUTES.NOT_FOUND,
        element: <NotFoundPage />,
        handle: { titleKey: 'routes.notFound' } as BreadcrumbHandle,
      },
    ],
  },
]);

export default router;
