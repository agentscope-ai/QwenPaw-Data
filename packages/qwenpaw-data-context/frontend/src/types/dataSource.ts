export type DataSourceType =
  | 'bigquery'
  | 'doris'
  | 'duckdb'
  | 'mysql'
  | 'odps'
  | 'postgresql'
  | 'sqlite'
  | 'starrocks'
  | 'tidb';

export interface MySqlDataSourceConfig {
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
}

export interface PostgreSqlDataSourceConfig {
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
}

export interface OdpsDataSourceConfig {
  access_key_id: string;
  access_key_secret: string;
  project: string;
  endpoint: string;
  sts_token: string | null;
}

export interface SqliteDataSourceConfig {
  path: string;
  read_only: boolean;
}

export interface BigQueryDataSourceConfig {
  project: string;
  credentials_json: string;
  location: string | null;
  maximum_bytes_billed: number | null;
}

export type DataSourceConfig =
  | MySqlDataSourceConfig
  | PostgreSqlDataSourceConfig
  | OdpsDataSourceConfig
  | SqliteDataSourceConfig
  | BigQueryDataSourceConfig;

export interface DataSourceCreateRequest {
  datasource_name: string;
  datasource_type: DataSourceType;
  config: DataSourceConfig;
  description?: string | null;
}

export type DataSourceUpdateRequest = Partial<DataSourceCreateRequest>;

export interface DataSourceResponse extends DataSourceCreateRequest {
  id: number;
  datasource_id: string;
}

export type DataSourceItem = DataSourceResponse;

export interface DataSourceQueryParams {
  datasource_id?: string;
  datasource_name?: string;
  datasource_type?: DataSourceType;
  page?: number;
  size?: number;
}

export type DataSourceConnectionTestRequest = Pick<
  DataSourceCreateRequest,
  'datasource_type' | 'config'
>;

export interface DataSourceConnectionTestResult {
  success: boolean;
  message: string;
  tables_found: number | null;
  elapsed_ms: number;
}

/** Form-field schema returned by the existing data-source type catalog. */
export interface FormSchemaProperty {
  title?: string;
  description?: string;
  type?: string;
  format?: string;
  default?: unknown;
  const?: string;
  anyOf?: Array<{ type?: string }>;
  'x-ui-group'?: string;
  'x-ui-order'?: number;
  'x-ui-placeholder'?: string;
}

export interface DataSourceFormSchema {
  properties: Record<string, FormSchemaProperty>;
  required?: string[];
}

export interface DataSourceTypeInfo {
  type: DataSourceType;
  label: string;
  category: 'sql';
  form_schema: DataSourceFormSchema | null;
}
