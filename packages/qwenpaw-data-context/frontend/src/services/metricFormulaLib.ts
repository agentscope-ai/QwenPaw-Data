import type {
  MetricFormulaCreateRequest,
  MetricFormulaResponse,
  MetricFormulaUpdateRequest,
  QueryMetricFormulaLibParams,
} from '@/types/metricFormulaLib';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const METRIC_FORMULA_API = semanticConfigApi('/metric-formula-lib');
const FORMULA_CREATE_FIELDS = [
  'metric_id',
  'dataset_id',
  'expr',
  'time_window',
  'additivity',
  'is_default',
] as const;
const FORMULA_UPDATE_FIELDS = ['expr', 'time_window', 'additivity', 'is_default'] as const;

export const createMetricFormula = (
  data: MetricFormulaCreateRequest,
): Promise<MetricFormulaResponse> => {
  return request.post(METRIC_FORMULA_API, pickSemanticFields(data, FORMULA_CREATE_FIELDS));
};

export const createMetricFormulaBatch = (data: MetricFormulaCreateRequest[]) => {
  return Promise.all(data.map(createMetricFormula));
};

export const queryMetricFormulaList = async (
  params: QueryMetricFormulaLibParams = {},
): Promise<SemanticConfigPage<MetricFormulaResponse>> => {
  const response = await request.get<unknown>(METRIC_FORMULA_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<MetricFormulaResponse>(response);
};

export const updateMetricFormula = (
  id: number,
  data: MetricFormulaUpdateRequest,
): Promise<MetricFormulaResponse> => {
  return request.put(
    `${METRIC_FORMULA_API}/${id}`,
    pickSemanticFields(data, FORMULA_UPDATE_FIELDS),
  );
};

export const deleteMetricFormula = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${METRIC_FORMULA_API}/${id}`);
};
