
// 列管理数据
export interface ColumnItem {
  // 主键（只读）
  id: number;
  // 前端生成的临时唯一标识（用于表格编辑）
  idx?: string | number;
  // 所属数据集 ID
  dataset_id: number;
  // 数据集名称（展示用，只读）
  dataset_name: string;
  // 数据源 ID
  datasource_id?: string;
  // 业务域 ID
  domain_id?: number;
  // 数据源名称（展示用）
  datasource_name?: string;
  // 业务域名称（展示用）
  domain_name?: string;
  // 列名
  column_name: string;
  // 列中文名
  column_name_cn?: string;
  // 数据类型
  data_type?: string;
  // 列注释
  column_comment?: string;
  // 列类型
  column_type?: string;
  // 枚举值（如 JSON/逗号分隔）
  column_enums?: string;
  // 枚举说明
  column_enums_description?: string;
  // 维度类型（键值列/级联维度/OLAP 维度/普通维度等）
  dimension_type?: string | null;
  // 样本值
  samples?: string;
}

// 查询列管理参数
export interface QueryColumnParams {
  datasource_id?: string;
  domain_id?: number;
  dataset_id?: number;
  column_name?: string;
  column_type?: string;
  dimension_type?: string;
  page?: number;
  size?: number;
}
