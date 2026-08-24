
// 指标公式库数据
export interface MetricFormulaLibItem {
  // 主键
  id: number;
  // 指标 ID（关联 metric_lib）
  metric_id?: number;
  // 指标名称（展示用）
  metric_name?: string;
  // 数据源 ID
  datasource_id?: string;
  // 业务域 ID
  domain_id?: number;
  // 数据源名称（展示用）
  datasource_name?: string;
  // 业务域名称（展示用）
  domain_name?: string;
  // 域（兼容旧文本字段）
  domain?: string;
  // 公式
  formula?: string;
  // 日期范围
  date_range?: string;
  // 数据集 ID
  dataset_id?: number;
  // 数据集名称（展示用）
  dataset_name?: string;
  // 兼容旧字段
  dataset?: string;
  // 公式依据
  formula_evidence?: string;
  // 来源
  derived_from?: string;
  // 扩展依据
  evidence_ext?: string;
}

// 查询指标公式库参数（过滤条件：datasource_id / domain_id / metric_id）
export interface QueryMetricFormulaLibParams {
  [key: string]: unknown;
  datasource_id?: string;
  domain_id?: number;
  metric_id?: number;
  dataset_id?: number;
  dataset?: string;
  date_range?: string;
  page?: number;
  size?: number;
}
