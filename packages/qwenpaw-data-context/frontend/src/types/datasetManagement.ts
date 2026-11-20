export interface DatasetCreateRequest {
  datasource_id: string;
  domain_id: number;
  dataset_name: string;
  description: string;
  source: string;
}

export type DatasetUpdateRequest = Pick<
  DatasetCreateRequest,
  'dataset_name' | 'description' | 'source'
>;

export interface DatasetResponse extends Omit<DatasetCreateRequest, 'datasource_id'> {
  dataset_id: number;
  datasource_id: string | null;
  datasource_name: string;
  domain_name: string;
}

export type DatasetManagementItem = DatasetResponse;

export interface QueryDatasetMetaParams {
  datasource_id?: string;
  domain_id?: number;
  dataset_name?: string;
  page?: number;
  size?: number;
}
