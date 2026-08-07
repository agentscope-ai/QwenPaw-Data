export interface BusinessDomainItem {
  datasource_id: string;
  domain_id: number;
  datasource_name: string;
  domain_name: string;
  display_name?: string;
  description: string;
  aliases?: string;
}

export interface BusinessDomainQueryParams {
  datasource_id?: string;
  domain_name?: string;
  page?: number;
  size?: number;
}
