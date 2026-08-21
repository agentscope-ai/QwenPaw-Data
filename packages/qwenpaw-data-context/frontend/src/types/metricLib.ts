
// 指标库数据
export interface MetricLibItem {
  // 主键
  id?: number;
  // 指标名称
  metric_name: string;
  // 数据源 ID
  datasource_id?: string;
  // 业务域 ID
  domain_id?: number;
  // 数据源名称（展示用）
  datasource_name?: string;
  // 业务域名称（展示用）
  domain_name?: string;
  // 描述
  description?: string;
  // 单位
  unit?: string;
  // 是否北极星指标
  is_polaris?: boolean;
  // 是否展示分布
  show_distribution?: boolean;
  // 是否可见
  is_visible?: boolean;
  // 同义词
  synonyms?: string;
  // 标签
  tags?: string;
  // 数据集ID（兼容旧字段）
  dataset_id?: string;
  // 数据集名称
  dataset_name?: string;
  // 域（兼容旧文本字段，逐步废弃）
  domain?: string;
}

// 查询指标库参数
export interface QueryMetricLibParams {
  metric_name?: string;
  datasource_id?: string;
  domain_id?: number;
  synonyms?: string;
  page?: number;
  size?: number;
}

// 指标重复检查结果
export interface MetricDuplicate {
  existing_metric: string;
  similarity_judgment: string;
  reason: string;
  suggestion: string;
}

export interface MetricCheckResult {
  input_metric: string;
  domain?: string;
  suggestion: string;
}
