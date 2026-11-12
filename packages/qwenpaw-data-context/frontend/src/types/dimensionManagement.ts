export type DimensionPriority = 'high' | 'normal';

export interface DimensionCreateRequest {
  domain_id: number; // 所属业务域 ID
  dimension_name: string; // 维度名称
  description?: string | null; // 描述
  synonyms?: string | null; // 同义词
  dimension_type?: string | null; // 维度类型
  child_dimensions?: string | null; // 子维度 ID 列表字符串
  priority?: DimensionPriority; // 优先级
}

export type DimensionUpdateRequest = Partial<
  Pick<
    DimensionCreateRequest,
    | 'dimension_name'
    | 'description'
    | 'synonyms'
    | 'dimension_type'
    | 'child_dimensions'
    | 'priority'
  >
>;

export interface DimensionResponse extends DimensionCreateRequest {
  dimension_id: number; // 维度 ID
  domain_name: string; // 业务域名称
  priority: DimensionPriority; // 优先级
}

export type DimensionItem = DimensionResponse;

export interface QueryDimensionParams {
  datasource_id?: string; // 工作台使用的数据源范围过滤，独立字典页不传
  domain_id?: number; // 所属业务域 ID
  dimension_name?: string; // 维度名称
  page?: number; // 页码
  size?: number; // 每页数量
}

export interface DimensionCaliberCreateRequest {
  dataset_id: number; // 关联数据集 ID
  dimension_id: number; // 维度 ID
  expr: string; // 计算表达式
  value_example?: string | null; // 取值示例
}

export type DimensionCaliberUpdateRequest = Pick<
  DimensionCaliberCreateRequest,
  'expr' | 'value_example'
>;

export interface DimensionCaliberResponse extends DimensionCaliberCreateRequest {
  datasource_id: string; // 由关联数据集派生，只读
  datasource_name: string; // 数据源展示名称，只读
  domain_id: number; // 由关联数据集派生，只读
  id: number; // 口径 ID
  dataset_name: string; // 数据集名称
  dimension_name: string; // 维度名称
  domain_name: string; // 业务域名称
}

export type DimensionCaliberItem = DimensionCaliberResponse;

export interface QueryDimensionCaliberParams {
  datasource_id?: string; // 工作台使用的数据源范围过滤，独立字典页不传
  domain_id?: number; // 所属业务域 ID
  dataset_id?: number; // 关联数据集 ID
  dataset_name?: string; // 数据集名称
  dimension_name?: string; // 维度名称
  page?: number; // 页码
  size?: number; // 每页数量
}

export type DimensionCaliberUpsertInput = DimensionCaliberCreateRequest & {
  id?: number; // 口径 ID
};
