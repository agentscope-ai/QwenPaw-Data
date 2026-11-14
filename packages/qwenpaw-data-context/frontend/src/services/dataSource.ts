import type {
  DataSourceConnectionTestRequest,
  DataSourceConnectionTestResult,
  DataSourceCreateRequest,
  DataSourceQueryParams,
  DataSourceResponse,
  DataSourceTypeInfo,
  DataSourceUpdateRequest,
} from '@/types/dataSource';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const DATA_SOURCE_API = semanticConfigApi('/datasource');
const DATA_SOURCE_TYPES_API = '/api/datasource-types';
const DATA_SOURCE_WRITE_FIELDS = [
  'datasource_name',
  'datasource_type',
  'config',
  'description',
] as const;

export const queryDataSourceList = async (
  params: DataSourceQueryParams = {},
): Promise<SemanticConfigPage<DataSourceResponse>> => {
  const response = await request.get<unknown>(DATA_SOURCE_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<DataSourceResponse>(response);
};

export const queryDataSourceTypeCatalog = async (): Promise<DataSourceTypeInfo[]> => {
  const response = await request.get<unknown>(DATA_SOURCE_TYPES_API);
  if (!Array.isArray(response)) {
    throw new Error('Invalid data-source type catalog response');
  }
  return response as DataSourceTypeInfo[];
};

export const getDataSource = (datasourceId: string): Promise<DataSourceResponse> => {
  return request.get(`${DATA_SOURCE_API}/${datasourceId}`);
};

export const createDataSource = (data: DataSourceCreateRequest): Promise<DataSourceResponse> => {
  return request.post(DATA_SOURCE_API, pickSemanticFields(data, DATA_SOURCE_WRITE_FIELDS));
};

export const updateDataSource = (
  datasourceId: string,
  data: DataSourceUpdateRequest,
): Promise<DataSourceResponse> => {
  return request.put(
    `${DATA_SOURCE_API}/${datasourceId}`,
    pickSemanticFields(data, DATA_SOURCE_WRITE_FIELDS),
  );
};

export const testDataSourceConnection = (
  data: DataSourceConnectionTestRequest,
): Promise<DataSourceConnectionTestResult> => {
  return request.post(
    semanticConfigApi('/datasource/test-connection'),
    pickSemanticFields(data, ['datasource_type', 'config'] as const),
  );
};

export const testSavedDataSourceConnection = async (
  datasourceId: string,
): Promise<DataSourceConnectionTestResult> => {
  const dataSource = await getDataSource(datasourceId);
  return testDataSourceConnection({
    datasource_type: dataSource.datasource_type,
    config: dataSource.config,
  });
};

export const deleteDataSource = (datasourceId: string): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${DATA_SOURCE_API}/${datasourceId}`);
};

export const setActiveDataSource = (datasourceId: string) => {
  return request.put('/api/datasources/active', { datasource_id: datasourceId });
};
