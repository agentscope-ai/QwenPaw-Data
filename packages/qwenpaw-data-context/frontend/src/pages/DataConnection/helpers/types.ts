import type { DataSourceType, DataSourceTypeInfo } from '@/services/dataConnection';

export interface DataConnectionTypeMeta {
  labelKey: string;
  badge: string;
  accent: string;
}

/** 连接页额外支持：MySQL 线协议兼容引擎、单文件数据库与 BigQuery。 */
export type DataConnectionType =
  | DataSourceType
  | 'starrocks'
  | 'doris'
  | 'tidb'
  | 'duckdb'
  | 'sqlite'
  | 'bigquery';

export const SUPPORTED_DATA_SOURCE_TYPES: DataSourceTypeInfo[] = [
  { type: 'mysql', defaultPort: 3306 },
  { type: 'postgresql', defaultPort: 5432 },
  { type: 'odps' },
];

export const DATA_CONNECTION_TYPE_META: Record<DataConnectionType, DataConnectionTypeMeta> = {
  mysql: {
    labelKey: 'dataConnection.types.mysql',
    badge: 'My',
    accent: '#0D76FD',
  },
  postgresql: {
    labelKey: 'dataConnection.types.postgresql',
    badge: 'PG',
    accent: '#722ed1',
  },
  odps: {
    labelKey: 'dataConnection.types.odps',
    badge: 'OD',
    accent: '#13c2c2',
  },
  starrocks: {
    labelKey: 'dataConnection.types.starrocks',
    badge: 'SR',
    accent: '#fa8c16',
  },
  doris: {
    labelKey: 'dataConnection.types.doris',
    badge: 'DR',
    accent: '#2f54eb',
  },
  tidb: {
    labelKey: 'dataConnection.types.tidb',
    badge: 'Ti',
    accent: '#eb2f96',
  },
  duckdb: {
    labelKey: 'dataConnection.types.duckdb',
    badge: 'Dk',
    accent: '#d4b106',
  },
  sqlite: {
    labelKey: 'dataConnection.types.sqlite',
    badge: 'SL',
    accent: '#003b57',
  },
  bigquery: {
    labelKey: 'dataConnection.types.bigquery',
    badge: 'BQ',
    accent: '#4285f4',
  },
};
