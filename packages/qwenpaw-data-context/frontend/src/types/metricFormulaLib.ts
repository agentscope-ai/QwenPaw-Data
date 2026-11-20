import type { MetricAdditivity } from './semanticConfig';

export interface MetricFormulaCreateRequest {
  metric_id: number; // 指标 ID
  dataset_id: number; // 关联数据集 ID
  expr: string; // 计算表达式
  time_window?: string | null; // 时间窗口
  additivity?: MetricAdditivity; // 可加性
  is_default?: boolean; // 是否默认口径
}

export type MetricFormulaUpdateRequest = Partial<
  Pick<MetricFormulaCreateRequest, 'expr' | 'time_window' | 'additivity' | 'is_default'>
>;

export interface MetricFormulaResponse extends MetricFormulaCreateRequest {
  datasource_id: string; // 由关联数据集派生，只读
  datasource_name: string; // 数据源展示名称，只读
  domain_id: number; // 由关联数据集派生，只读
  id: number; // 口径 ID
  metric_name: string; // 指标名称
  dataset_name: string; // 数据集名称
  domain_name: string; // 业务域名称
  additivity: MetricAdditivity; // 可加性
  is_default: boolean; // 是否默认口径
}

export type MetricFormulaLibItem = MetricFormulaResponse;

export interface QueryMetricFormulaLibParams {
  datasource_id?: string; // 工作台使用的数据源范围过滤，独立字典页不传
  domain_id?: number; // 所属业务域 ID
  metric_id?: number; // 指标 ID
  dataset_id?: number; // 关联数据集 ID
  page?: number; // 页码
  size?: number; // 每页数量
}
