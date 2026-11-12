import type {
  MetricCreateRequest,
  MetricResponse,
  MetricUpdateRequest,
  QueryMetricLibParams,
} from '@/types/metricLib';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const METRIC_API = semanticConfigApi('/metric-lib');
const METRIC_CREATE_FIELDS = [
  'domain_id',
  'metric_name',
  'description',
  'synonyms',
  'priority',
] as const;
const METRIC_UPDATE_FIELDS = ['metric_name', 'description', 'synonyms', 'priority'] as const;

export const createMetric = (data: MetricCreateRequest): Promise<MetricResponse> => {
  return request.post(METRIC_API, pickSemanticFields(data, METRIC_CREATE_FIELDS));
};

export const confirmCreateMetric = (data: MetricCreateRequest[]) => {
  return Promise.all(data.map(createMetric));
};

export const checkDuplicates = (data: MetricCreateRequest[]) => {
  void data;
  return Promise.resolve([]);
};

export const queryMetricList = async (
  params: QueryMetricLibParams = {},
): Promise<SemanticConfigPage<MetricResponse>> => {
  const response = await request.get<unknown>(METRIC_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<MetricResponse>(response);
};

export const updateMetric = (id: number, data: MetricUpdateRequest): Promise<MetricResponse> => {
  return request.put(`${METRIC_API}/${id}`, pickSemanticFields(data, METRIC_UPDATE_FIELDS));
};

export const deleteMetric = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${METRIC_API}/${id}`);
};
