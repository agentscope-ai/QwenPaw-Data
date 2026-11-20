export interface BusinessDomainCreateRequest {
  domain_name: string; // 业务域名称
  description: string; // 描述
}

export type BusinessDomainUpdateRequest = Pick<
  BusinessDomainCreateRequest,
  'domain_name' | 'description'
>;

export interface BusinessDomainResponse extends BusinessDomainCreateRequest {
  domain_id: number; // 所属业务域 ID
  datasource_scope?: string | null; // 后端派生的数据源范围，只读且不展示
}

export type BusinessDomainItem = BusinessDomainResponse;

export interface BusinessDomainQueryParams {
  domain_name?: string; // 业务域名称
  page?: number; // 页码
  size?: number; // 每页数量
}
