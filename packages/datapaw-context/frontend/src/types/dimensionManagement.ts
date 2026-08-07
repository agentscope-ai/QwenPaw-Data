
// ==================== 维度主表（/api/dimension）====================

// 维度主表数据
export interface DimensionItem {
  id: number;
  datasource_id?: string;
  domain_id?: number;
  datasource_name?: string;
  domain_name?: string;
  dimension_name: string;
  description?: string;
  parent_name?: string;
  depth?: number;
  synonyms?: string;
  is_visible?: boolean;
  is_attribution?: boolean;
  enums?: string;
}

// 查询维度主表参数
export interface QueryDimensionParams {
  [key: string]: unknown;
  datasource_id?: string;
  domain_id?: number;
  dimension_name?: string;
  page?: number;
  size?: number;
}

// ==================== 维度口径（/api/dataset-dimension）====================

// 维度口径数据
export interface DimensionCaliberItem {
  id: number;
  dataset_id: number;
  dataset_name?: string;
  dimension_id?: number;
  dimension_name?: string;
  datasource_id?: string;
  domain_id?: number;
  datasource_name?: string;
  domain_name?: string;
  calculate_expr?: string;
  dimension_type?: string | null;
  data_type?: string;
  // 以下字段在预览模式下可能携带
  domain?: string;
  synonyms?: string;
}

// 查询维度口径参数
export interface QueryDimensionCaliberParams {
  datasource_id?: string;
  domain_id?: number;
  dataset_id?: number;
  dataset_name?: string;
  dimension_name?: string;
  page?: number;
  size?: number;
}
