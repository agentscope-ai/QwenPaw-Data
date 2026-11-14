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

/** Resolve an exact object; never infer source/domain identity from a label. */
export async function getDatasetMeta(
  id: number,
  signal?: AbortSignal,
): Promise<import('../embedded/contracts').LocatedDataset> {
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('INVALID_ARGUMENT');
  const data = await request.get<Record<string, unknown>>(`${DATASET_API}/${id}`, {
    signal,
    redirectOnForbidden: false,
  });
  const source = data.datasource_id;
  if (
    data.dataset_id !== id ||
    !Number.isSafeInteger(data.domain_id) ||
    Number(data.domain_id) <= 0 ||
    typeof source !== 'string' ||
    !source.trim()
  )
    throw new Error('TARGET_NOT_FOUND');
  if (data.description != null && typeof data.description !== 'string')
    throw new Error('INVALID_ARGUMENT');
  const record: DatasetResponse = {
    dataset_id: id,
    domain_id: Number(data.domain_id),
    datasource_id: source,
    dataset_name: typeof data.dataset_name === 'string' ? data.dataset_name : '',
    description: (data.description as string | null) ?? '',
    source: typeof data.source === 'string' ? data.source : '',
    datasource_name: typeof data.datasource_name === 'string' ? data.datasource_name : '',
    domain_name: typeof data.domain_name === 'string' ? data.domain_name : '',
  };
  return {
    record,
    dataset_id: id,
    domain_id: Number(data.domain_id),
    datasource_id: source,
    dataset_name: typeof data.dataset_name === 'string' ? data.dataset_name : '',
    description: (data.description as string | null) ?? '',
  };
}

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
