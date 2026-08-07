export type DataSourceType = 'ODPS' | 'MYSQL' | 'POSTGRESQL' | 'odps' | 'mysql' | 'postgresql';

export type DataSourceConfig =
  | {
      host: string;
      port?: number;
      dbname: string;
      user: string;
      password: string;
    }
  | {
      host: string;
      port?: number;
      database: string;
      user: string;
      password: string;
    }
  | {
      access_key_id: string;
      access_key_secret: string;
      project: string;
      endpoint: string;
      sts_token?: string | null;
    }
  | Record<string, unknown>;

export interface DataSourceItem {
  datasource_id: string;
  datasource_name: string;
  datasource_type: DataSourceType;
  config?: DataSourceConfig | null;
}

export interface DataSourceQueryParams {
  [key: string]: unknown;
  datasource_id?: string;
  datasource_name?: string;
  datasource_type?: string;
  page?: number;
  size?: number;
}

export interface DataSourceConnectionTestResult {
  success: boolean;
  message: string;
  tables_found: number | null;
  elapsed_ms: number;
}
