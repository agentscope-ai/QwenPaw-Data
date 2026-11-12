import type {
  ColumnCreateRequest,
  ColumnResponse,
  ColumnUpdateRequest,
  ColumnUpsertInput,
  QueryColumnParams,
} from '@/types/column';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const COLUMN_API = semanticConfigApi('/dataset-column-meta');
const COLUMN_CREATE_FIELDS = [
  'datasource_id',
  'domain_id',
  'dataset_id',
  'field_name',
  'description',
  'data_type',
  'is_primary',
  'is_nullable',
] as const;
const COLUMN_UPDATE_FIELDS = [
  'field_name',
  'description',
  'data_type',
  'is_primary',
  'is_nullable',
] as const;

export const queryColumnList = async (
  params: QueryColumnParams = {},
): Promise<SemanticConfigPage<ColumnResponse>> => {
  const response = await request.get<unknown>(COLUMN_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<ColumnResponse>(response);
};

export const createColumn = (data: ColumnCreateRequest): Promise<ColumnResponse> => {
  return request.post(COLUMN_API, pickSemanticFields(data, COLUMN_CREATE_FIELDS));
};

export const updateColumn = (id: number, data: ColumnUpdateRequest): Promise<ColumnResponse> => {
  return request.put(`${COLUMN_API}/${id}`, pickSemanticFields(data, COLUMN_UPDATE_FIELDS));
};

export const deleteColumn = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${COLUMN_API}/${id}`);
};

export const previewColumnMeta = (datasetId: number): Promise<ColumnResponse[]> => {
  return request.get(`${COLUMN_API}/dataset/${datasetId}`);
};

const persistColumns = (data: ColumnUpsertInput[]) => {
  return Promise.all(
    data.map((item) =>
      item.id == null
        ? createColumn(pickSemanticFields(item, COLUMN_CREATE_FIELDS))
        : updateColumn(item.id, pickSemanticFields(item, COLUMN_UPDATE_FIELDS)),
    ),
  );
};

export const confirmColumnMetaStorage = (_datasetId: number, data: ColumnUpsertInput[]) =>
  persistColumns(data);

export const inferDimensions = (_datasetId: number, data: ColumnUpsertInput[]) => {
  return Promise.resolve(data);
};

export const confirmDimensions = (_datasetId: number, data: ColumnUpsertInput[]) =>
  persistColumns(data);

export const completeSamples = (datasetId: number) => previewColumnMeta(datasetId);

export const confirmSamples = (_datasetId: number, data: ColumnUpsertInput[]) =>
  persistColumns(data);
