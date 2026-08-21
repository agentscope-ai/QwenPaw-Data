import { request } from '@/utils/request';
import type { DataSourceConnectionTestResult, DataSourceItem, DataSourceQueryParams } from '@/types/dataSource';
import { semanticConfigApi } from './api';

// 查询数据源列表
export const queryDataSourceList = (params?: DataSourceQueryParams) => {
  return request.get(semanticConfigApi('/datasource'), { params });
};

// Credential-free datasource identities for ordinary query pages/dropdowns.
export const queryDataSourceMetadata = (params?: DataSourceQueryParams) => {
  return request.get('/api/v1/cm/datasources', { params });
};

// 新增数据源
export const createDataSource = (data: Partial<DataSourceItem>) => {
  const payload = { ...data };
  delete payload.datasource_id;
  return request.post(semanticConfigApi('/datasource'), payload);
};

// 编辑数据源
export const updateDataSource = (datasourceId: string, data: Partial<DataSourceItem>) => {
  const payload = { ...data };
  delete payload.datasource_id;
  return request.put(semanticConfigApi(`/datasource/${datasourceId}`), payload);
};

// 测试未保存的数据源连接
export const testDataSourceConnection = (
  data: Pick<Partial<DataSourceItem>, 'datasource_type' | 'config'>
): Promise<DataSourceConnectionTestResult> => {
  return request.post(semanticConfigApi('/datasource/test-connection'), data);
};

// 测试已保存的数据源连接
export const testSavedDataSourceConnection = (
  datasourceId: string
): Promise<DataSourceConnectionTestResult> => {
  return request.post(semanticConfigApi(`/datasource/${datasourceId}/test-connection`));
};

// 删除数据源
export const deleteDataSource = (datasourceId: string) => {
  return request.delete(semanticConfigApi(`/datasource/${datasourceId}`));
};
