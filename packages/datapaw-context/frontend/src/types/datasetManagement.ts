
// 查询数据集元数据参数
export interface QueryDatasetMetaParams {
  [key: string]: unknown;
  datasource_id?: string;
  domain_id?: number;
  dataset_name?: string;
  dataset_type?: string;
  page?: number;
  size?: number;
}

// 数据集管理数据
export interface DatasetManagementItem {
  dataset_id: number;
  datasource_id?: string;
  domain_id?: number;
  datasource_name?: string;
  domain_name?: string;
  // 数据集名称
  dataset_name: string;
  // 数据集描述
  dataset_comment?: string;
  // 数据集类型
  dataset_type?: string | null;
  // SQL内容
  sql_content?: string;
  // 父级数据集
  parents?: string;
}
