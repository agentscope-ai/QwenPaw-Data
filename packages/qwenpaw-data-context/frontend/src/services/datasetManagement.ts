import type {
  DatasetCreateRequest,
  DatasetResponse,
  DatasetUpdateRequest,
  QueryDatasetMetaParams,
} from '@/types/datasetManagement';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const DATASET_API = semanticConfigApi('/dataset-meta');

export const queryDatasetMeta = async (
  params: QueryDatasetMetaParams = {},
): Promise<SemanticConfigPage<DatasetResponse>> => {
  const response = await request.get<unknown>(DATASET_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<DatasetResponse>(response);
};

export const createDatasetMeta = (data: DatasetCreateRequest): Promise<DatasetResponse> => {
  return request.post(
    DATASET_API,
    pickSemanticFields(data, [
      'datasource_id',
      'domain_id',
      'dataset_name',
      'description',
      'source',
    ] as const),
  );
};

export const updateDatasetMeta = (
  id: number,
  data: DatasetUpdateRequest,
): Promise<DatasetResponse> => {
  return request.put(
    `${DATASET_API}/${id}`,
    pickSemanticFields(data, ['dataset_name', 'description', 'source'] as const),
  );
};

export const deleteDatasetMeta = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${DATASET_API}/${id}`);
};
