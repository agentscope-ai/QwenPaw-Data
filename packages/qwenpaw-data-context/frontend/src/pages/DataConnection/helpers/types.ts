import type { DataSourceType, DataSourceTypeInfo } from "@/services/dataConnection";

export interface DataConnectionTypeMeta {
  labelKey: string;
  badge: string;
  accent: string;
}

export const SUPPORTED_DATA_SOURCE_TYPES: DataSourceTypeInfo[] = [
  { type: "mysql", defaultPort: 3306 },
  { type: "postgresql", defaultPort: 5432 },
  { type: "odps" },
];

export const DATA_CONNECTION_TYPE_META: Record<DataSourceType, DataConnectionTypeMeta> = {
  mysql: {
    labelKey: "dataConnection.types.mysql",
    badge: "My",
    accent: "#0D76FD",
  },
  postgresql: {
    labelKey: "dataConnection.types.postgresql",
    badge: "PG",
    accent: "#722ed1",
  },
  odps: {
    labelKey: "dataConnection.types.odps",
    badge: "OD",
    accent: "#13c2c2",
  },
};
