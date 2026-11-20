export interface SemanticConfigPage<T> {
  records: T[];
  total: number;
  page: number;
  size: number;
}

export interface SemanticConfigErrorBody {
  timestamp: string;
  status: number;
  error: string;
  message: string;
}

export interface SemanticConfigDeleteResponse {
  message: string;
  id?: string | number;
  datasource_id?: string;
}

export const SEMANTIC_PRIORITY_VALUES = ['HIGH', 'MEDIUM', 'LOW'] as const;
export type SemanticPriority = (typeof SEMANTIC_PRIORITY_VALUES)[number];

export const SEMANTIC_YES_NO_VALUES = ['Y', 'N'] as const;
export type SemanticYesNo = (typeof SEMANTIC_YES_NO_VALUES)[number];

export const METRIC_ADDITIVITY_VALUES = ['ADDITIVE', 'SEMI_ADDITIVE', 'NON_ADDITIVE'] as const;
export type MetricAdditivity = (typeof METRIC_ADDITIVITY_VALUES)[number];
