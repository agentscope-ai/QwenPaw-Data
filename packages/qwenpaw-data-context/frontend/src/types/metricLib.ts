import type { SemanticPriority } from './semanticConfig';

export interface MetricCreateRequest {
  domain_id: number; // 所属业务域 ID
  metric_name: string; // 指标名称
  description?: string | null; // 描述
  synonyms?: string | null; // 同义词
  priority?: SemanticPriority; // 优先级
}

export type MetricUpdateRequest = Partial<
  Pick<MetricCreateRequest, 'metric_name' | 'description' | 'synonyms' | 'priority'>
>;

export interface MetricResponse extends MetricCreateRequest {
  metric_id: number; // 指标 ID
  domain_name: string; // 业务域名称
  priority: SemanticPriority; // 优先级
}

export type MetricLibItem = MetricResponse;

export interface QueryMetricLibParams {
  metric_name?: string; // 指标名称
  datasource_id?: string; // 工作台使用的数据源范围过滤，独立字典页不传
  domain_id?: number; // 所属业务域 ID
  page?: number; // 页码
  size?: number; // 每页数量
}

export interface MetricCheckResult {
  input_metric: string; // 待检查指标
  suggestion: string; // 检查建议
}
