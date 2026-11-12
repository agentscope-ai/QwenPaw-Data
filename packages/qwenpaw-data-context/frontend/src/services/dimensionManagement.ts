import type {
  DimensionCaliberCreateRequest,
  DimensionCaliberResponse,
  DimensionCaliberUpdateRequest,
  DimensionCaliberUpsertInput,
  DimensionCreateRequest,
  DimensionResponse,
  DimensionUpdateRequest,
  QueryDimensionCaliberParams,
  QueryDimensionParams,
} from '@/types/dimensionManagement';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const DIMENSION_API = semanticConfigApi('/dimension');
const DIMENSION_CALIBER_API = semanticConfigApi('/dataset-dimension');
const DIMENSION_CREATE_FIELDS = [
  'domain_id',
  'dimension_name',
  'description',
  'synonyms',
  'dimension_type',
  'child_dimensions',
  'priority',
] as const;
const DIMENSION_UPDATE_FIELDS = [
  'dimension_name',
  'description',
  'synonyms',
  'dimension_type',
  'child_dimensions',
  'priority',
] as const;
const CALIBER_CREATE_FIELDS = ['dataset_id', 'dimension_id', 'expr', 'value_example'] as const;
const CALIBER_UPDATE_FIELDS = ['expr', 'value_example'] as const;

export const queryDimensionList = async (
  params: QueryDimensionParams = {},
): Promise<SemanticConfigPage<DimensionResponse>> => {
  const response = await request.get<unknown>(DIMENSION_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<DimensionResponse>(response);
};

export const createDimension = (data: DimensionCreateRequest): Promise<DimensionResponse> => {
  return request.post(DIMENSION_API, pickSemanticFields(data, DIMENSION_CREATE_FIELDS));
};

export const updateDimension = (
  id: number,
  data: DimensionUpdateRequest,
): Promise<DimensionResponse> => {
  return request.put(`${DIMENSION_API}/${id}`, pickSemanticFields(data, DIMENSION_UPDATE_FIELDS));
};

export const deleteDimension = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${DIMENSION_API}/${id}`);
};

export const queryDimensionCaliberList = async (
  params: QueryDimensionCaliberParams = {},
): Promise<SemanticConfigPage<DimensionCaliberResponse>> => {
  const response = await request.get<unknown>(DIMENSION_CALIBER_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<DimensionCaliberResponse>(response);
};

export const getDimensionCaliber = (id: number): Promise<DimensionCaliberResponse> => {
  return request.get(`${DIMENSION_CALIBER_API}/${id}`);
};

export const getDimensionCalibersByDataset = (
  datasetId: number,
): Promise<DimensionCaliberResponse[]> => {
  return request.get(`${DIMENSION_CALIBER_API}/dataset/${datasetId}`);
};

export const createDimensionCaliber = (
  data: DimensionCaliberCreateRequest,
): Promise<DimensionCaliberResponse> => {
  return request.post(DIMENSION_CALIBER_API, pickSemanticFields(data, CALIBER_CREATE_FIELDS));
};

export const updateDimensionCaliber = (
  id: number,
  data: DimensionCaliberUpdateRequest,
): Promise<DimensionCaliberResponse> => {
  return request.put(
    `${DIMENSION_CALIBER_API}/${id}`,
    pickSemanticFields(data, CALIBER_UPDATE_FIELDS),
  );
};

export const deleteDimensionCaliber = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${DIMENSION_CALIBER_API}/${id}`);
};

export const previewDimension = (datasetIds: number[]) => {
  return Promise.all(datasetIds.map(getDimensionCalibersByDataset)).then((results) =>
    results.flat(),
  );
};

export const confirmDimensionStorage = (data: DimensionCaliberUpsertInput[]) => {
  return Promise.all(
    data.map((item) =>
      item.id == null
        ? createDimensionCaliber(pickSemanticFields(item, CALIBER_CREATE_FIELDS))
        : updateDimensionCaliber(item.id, pickSemanticFields(item, CALIBER_UPDATE_FIELDS)),
    ),
  );
};
