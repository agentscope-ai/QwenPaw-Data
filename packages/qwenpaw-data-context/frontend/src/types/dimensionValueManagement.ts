
// 维度值管理数据
export interface DimensionValueItem {
  // 主键
  id: number;
  // 可编辑行 key（前端临时字段）
  _key?: string;
  // 所属数据集 ID
  dataset_id: number;
  // 数据集名称（只读）
  dataset_name?: string;
  // 所属维度 ID
  dimension_id: number;
  // 维度名称（只读）
  dimension_name?: string;
  // 数据源 ID
  datasource_id?: string;
  // 业务域 ID
  domain_id?: number;
  // 数据源名称（展示用，只读）
  datasource_name?: string;
  // 业务域名称（展示用，只读）
  domain_name?: string;
  // 计算表达式
  calculate_expr?: string;
  // 维度类型
  dimension_type?: string;
  // 数据类型
  data_type?: string;
  // 维度值
  dimension_value: string;
  // 维度出现次数
  dimension_occur_cnt?: number;
}

// 查询维度值参数
export interface QueryDimensionValueParams {
  [key: string]: unknown;
  datasource_id?: string;
  domain_id?: number;
  datasetId?: number;
  dimensionId?: number;
  dimensionName?: string;
  dimensionValue?: string;
  page?: number;
  size?: number;
}
