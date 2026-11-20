import assert from 'node:assert/strict';
import {
  METRIC_ADDITIVITY_VALUES,
  SEMANTIC_PRIORITY_VALUES,
  SEMANTIC_YES_NO_VALUES,
} from '../types/semanticConfig.ts';
import {
  assertSemanticConfigPage,
  isSemanticConfigErrorBody,
  pickSemanticFields,
} from './semanticConfigContract.ts';
import { parseUrlNumber, readUrlSearchValues } from './urlSearch.ts';

await import('./semanticDeepLink.test.ts');
await import('../services/excelImport.test.ts');
await import('../services/apiError.test.ts');
await import('../pages/SemanticWeaving/semanticWeaving.test.ts');

interface CapturedRequest {
  method: 'get' | 'post' | 'put' | 'delete';
  url: string;
  data?: unknown;
  config?: { params?: Record<string, unknown> };
}

interface RequestTestHarness {
  requestTestCalls: CapturedRequest[];
  resetRequestTestHarness: () => void;
  setRequestTestResponse: (value: unknown) => void;
}

// Provided by semanticConfigContract.loader.mjs for this test process only.
// @ts-expect-error test-only virtual module
const requestHarness = (await import('semantic-test:request')) as RequestTestHarness;

const page = assertSemanticConfigPage<{ id: number }>({
  records: [{ id: 1 }],
  total: 1,
  page: 1,
  size: 20,
});
assert.deepEqual(page.records, [{ id: 1 }]);
assert.throws(
  () => assertSemanticConfigPage([{ id: 1 }]),
  /Invalid semantic-config pagination response/,
);
assert.throws(
  () => assertSemanticConfigPage({ data: [], total: 0 }),
  /Invalid semantic-config pagination response/,
);

assert.equal(
  isSemanticConfigErrorBody({
    timestamp: '2026-06-22T02:00:00Z',
    status: 400,
    error: 'Bad Request',
    message: 'field_name 不能为空',
  }),
  true,
);
assert.equal(isSemanticConfigErrorBody({ status: 400, message: 'legacy' }), false);

const writeCases = [
  pickSemanticFields(
    {
      datasource_name: 'source',
      datasource_type: 'mysql',
      config: {},
      description: 'desc',
      datasource_id: 'must-not-write',
    },
    ['datasource_name', 'datasource_type', 'config', 'description'] as const,
  ),
  pickSemanticFields(
    { datasource_id: 'mysql-1', domain_name: 'domain', description: 'desc', aliases: 'legacy' },
    ['domain_name', 'description'] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_name: 'dataset',
      description: 'desc',
      source: 'select 1',
      dataset_comment: 'legacy',
    },
    ['datasource_id', 'domain_id', 'dataset_name', 'description', 'source'] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_id: 2,
      field_name: 'field',
      description: 'desc',
      data_type: 'STRING',
      is_primary: 'N',
      is_nullable: 'Y',
      column_name: 'legacy',
    },
    [
      'datasource_id',
      'domain_id',
      'dataset_id',
      'field_name',
      'description',
      'data_type',
      'is_primary',
      'is_nullable',
    ] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dimension_name: 'dimension',
      dimension_type: '普通维度',
      child_dimensions: 'a$$$b',
      priority: 'MEDIUM',
      parent_name: 'legacy',
    },
    ['domain_id', 'dimension_name', 'dimension_type', 'child_dimensions', 'priority'] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_id: 2,
      dimension_id: 3,
      expr: 'field',
      value_example: 'A$$$B',
      calculate_expr: 'legacy',
    },
    ['dataset_id', 'dimension_id', 'expr', 'value_example'] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      metric_name: 'GMV',
      description: 'desc',
      synonyms: '成交额',
      priority: 'HIGH',
      unit: 'legacy',
    },
    ['domain_id', 'metric_name', 'description', 'synonyms', 'priority'] as const,
  ),
  pickSemanticFields(
    {
      datasource_id: 'mysql-1',
      domain_id: 1,
      metric_id: 4,
      dataset_id: 2,
      expr: 'SUM(amount)',
      time_window: '1d',
      additivity: 'ADDITIVE',
      is_default: true,
      formula: 'legacy',
    },
    ['metric_id', 'dataset_id', 'expr', 'time_window', 'additivity', 'is_default'] as const,
  ),
];

const legacyFields = new Set([
  'aliases',
  'dataset_comment',
  'column_name',
  'parent_name',
  'calculate_expr',
  'unit',
  'formula',
]);
for (const payload of writeCases) {
  for (const key of Object.keys(payload)) {
    assert.equal(legacyFields.has(key), false, `legacy field leaked: ${key}`);
  }
}

assert.deepEqual(SEMANTIC_PRIORITY_VALUES, ['HIGH', 'MEDIUM', 'LOW']);
assert.deepEqual(SEMANTIC_YES_NO_VALUES, ['Y', 'N']);
assert.deepEqual(METRIC_ADDITIVITY_VALUES, ['ADDITIVE', 'SEMI_ADDITIVE', 'NON_ADDITIVE']);

const canonicalDeepLink = readUrlSearchValues<{
  datasource_id: string;
  domain_id: number;
  dataset_id: number;
}>(
  new URLSearchParams(
    'datasource_id=mysql-1&domain_id=12&dataset_id=bad&domainId=999&unknown=value',
  ),
  {
    datasource_id: {},
    domain_id: { parse: parseUrlNumber },
    dataset_id: { parse: parseUrlNumber },
  },
);
assert.deepEqual(canonicalDeepLink, {
  datasource_id: 'mysql-1',
  domain_id: 12,
});
assert.deepEqual(
  readUrlSearchValues<{ domain_id: number }>(new URLSearchParams('domainId=999'), {
    domain_id: { parse: parseUrlNumber },
  }),
  {},
);

const pageResponse = { records: [], total: 0, page: 1, size: 20 };
const dataSourceService = await import('../services/dataSource.ts');
const businessDomainService = await import('../services/businessDomain.ts');
const datasetService = await import('../services/datasetManagement.ts');
const columnService = await import('../services/column.ts');
const dimensionService = await import('../services/dimensionManagement.ts');
const metricService = await import('../services/metricLib.ts');
const metricFormulaService = await import('../services/metricFormulaLib.ts');
const { parseApiError } = await import('../services/apiError.ts');

const listCases = [
  [dataSourceService.queryDataSourceList, '/api/semantic-config/datasource'],
  [businessDomainService.queryBusinessDomainList, '/api/semantic-config/biz-domain'],
  [datasetService.queryDatasetMeta, '/api/semantic-config/dataset-meta'],
  [columnService.queryColumnList, '/api/semantic-config/dataset-column-meta'],
  [dimensionService.queryDimensionList, '/api/semantic-config/dimension'],
  [dimensionService.queryDimensionCaliberList, '/api/semantic-config/dataset-dimension'],
  [metricService.queryMetricList, '/api/semantic-config/metric-lib'],
  [metricFormulaService.queryMetricFormulaList, '/api/semantic-config/metric-formula-lib'],
] as const;

requestHarness.resetRequestTestHarness();
for (const [query, expectedUrl] of listCases) {
  requestHarness.setRequestTestResponse(pageResponse);
  await query({});
  const call = requestHarness.requestTestCalls.shift();
  assert.ok(call, `missing request call for ${expectedUrl}`);
  assert.equal(call.method, 'get');
  assert.equal(call.url, expectedUrl);
  assert.deepEqual(call.config?.params, { page: 1, size: 20 });
}

// 旧深链/调用方不能再把业务域按数据源过滤；字典仍支持工作台派生范围。
const legacyDomainQuery = { datasource_id: 'mysql-1', domain_name: 'sales', page: 2, size: 50 };
await businessDomainService.queryBusinessDomainList(legacyDomainQuery);
assert.deepEqual(requestHarness.requestTestCalls.shift()?.config?.params, {
  domain_name: 'sales',
  page: 2,
  size: 50,
});
for (const query of [dimensionService.queryDimensionList, metricService.queryMetricList]) {
  await query({ datasource_id: 'mysql-1', domain_id: 1 });
  assert.deepEqual(requestHarness.requestTestCalls.shift()?.config?.params, {
    datasource_id: 'mysql-1',
    domain_id: 1,
    page: 1,
    size: 20,
  });
}

const createCases: ReadonlyArray<{
  create: (data: never) => Promise<unknown>;
  url: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
}> = [
  {
    create: dataSourceService.createDataSource,
    url: '/api/semantic-config/datasource',
    input: {
      datasource_name: 'source',
      datasource_type: 'mysql',
      config: { host: '127.0.0.1' },
      description: null,
      datasource_id: 'legacy',
    },
    expected: {
      datasource_name: 'source',
      datasource_type: 'mysql',
      config: { host: '127.0.0.1' },
      description: null,
    },
  },
  {
    create: businessDomainService.createBusinessDomain,
    url: '/api/semantic-config/biz-domain',
    input: {
      datasource_id: 'mysql-1',
      domain_name: 'sales',
      description: 'desc',
      aliases: 'legacy',
    },
    expected: { domain_name: 'sales', description: 'desc' },
  },
  {
    create: datasetService.createDatasetMeta,
    url: '/api/semantic-config/dataset-meta',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_name: 'orders',
      description: 'desc',
      source: 'select 1',
      dataset_comment: 'legacy',
    },
    expected: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_name: 'orders',
      description: 'desc',
      source: 'select 1',
    },
  },
  {
    create: columnService.createColumn,
    url: '/api/semantic-config/dataset-column-meta',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_id: 2,
      field_name: 'amount',
      description: 'desc',
      data_type: 'DECIMAL',
      is_primary: 'N',
      is_nullable: 'Y',
      column_name: 'legacy',
    },
    expected: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_id: 2,
      field_name: 'amount',
      description: 'desc',
      data_type: 'DECIMAL',
      is_primary: 'N',
      is_nullable: 'Y',
    },
  },
  {
    create: dimensionService.createDimension,
    url: '/api/semantic-config/dimension',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dimension_name: 'region',
      description: null,
      synonyms: null,
      dimension_type: 'categorical',
      child_dimensions: 'province$$$city',
      priority: 'normal',
      parent_name: 'legacy',
    },
    expected: {
      domain_id: 1,
      dimension_name: 'region',
      description: null,
      synonyms: null,
      dimension_type: 'categorical',
      child_dimensions: 'province$$$city',
      priority: 'normal',
    },
  },
  {
    create: dimensionService.createDimensionCaliber,
    url: '/api/semantic-config/dataset-dimension',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      dataset_id: 2,
      dimension_id: 3,
      expr: 'region_code',
      value_example: 'CN',
      calculate_expr: 'legacy',
    },
    expected: {
      dataset_id: 2,
      dimension_id: 3,
      expr: 'region_code',
      value_example: 'CN',
    },
  },
  {
    create: metricService.createMetric,
    url: '/api/semantic-config/metric-lib',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      metric_name: 'GMV',
      description: 'desc',
      synonyms: '成交额',
      priority: 'HIGH',
      unit: 'legacy',
    },
    expected: {
      domain_id: 1,
      metric_name: 'GMV',
      description: 'desc',
      synonyms: '成交额',
      priority: 'HIGH',
    },
  },
  {
    create: metricFormulaService.createMetricFormula,
    url: '/api/semantic-config/metric-formula-lib',
    input: {
      datasource_id: 'mysql-1',
      domain_id: 1,
      metric_id: 4,
      dataset_id: 2,
      expr: 'SUM(amount)',
      time_window: '1d',
      additivity: 'ADDITIVE',
      is_default: true,
      formula: 'legacy',
    },
    expected: {
      metric_id: 4,
      dataset_id: 2,
      expr: 'SUM(amount)',
      time_window: '1d',
      additivity: 'ADDITIVE',
      is_default: true,
    },
  },
];

for (const { create, url, input, expected } of createCases) {
  requestHarness.setRequestTestResponse(expected);
  await create(input as never);
  const call = requestHarness.requestTestCalls.shift();
  assert.ok(call, `missing create request call for ${url}`);
  assert.equal(call.method, 'post');
  assert.equal(call.url, url);
  assert.deepEqual(call.data, expected);
}

for (const status of [400, 404, 500]) {
  const body = {
    timestamp: '2026-08-23T06:00:00Z',
    status,
    error: status === 400 ? 'Bad Request' : 'Request Error',
    message: `semantic-config-${status}`,
  };
  const error = parseApiError(body, status);
  assert.equal(error.status, status);
  assert.equal(error.message, body.message);
  assert.deepEqual(error.semanticConfig, body);
}

console.log('semanticConfigContract: all assertions passed');
