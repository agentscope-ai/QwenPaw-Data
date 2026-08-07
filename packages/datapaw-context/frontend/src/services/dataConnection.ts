import { request } from "./request";

/** Supported data source types (aligned with backend API). */
export type DataSourceType = "mysql" | "postgresql" | "odps";

export interface MysqlPostgresConfig {
  host?: string;
  port?: number;
  user?: string;
  password?: string;
  db?: string;
}

export interface OdpsConfig {
  endpoint?: string;
  project_name?: string;
  access_id?: string;
  access_key?: string;
  app_name?: string;
}

export type DataSourceConnectionConfig = MysqlPostgresConfig & OdpsConfig;

export interface DataSourceRecord {
  id: string;
  type: DataSourceType;
  name: string;
  config: DataSourceConnectionConfig;
  createdAt: string;
  updatedAt: string;
}

export interface DataSourceCreatePayload {
  type: DataSourceType;
  name: string;
  config: DataSourceConnectionConfig;
}

export interface DataSourceTestPayload {
  type: DataSourceType;
  config: DataSourceConnectionConfig;
}

export interface DataSourceTestResult {
  success: boolean;
  message: string;
  latencyMs?: number;
}

export interface DataSourceListResponse {
  items: DataSourceRecord[];
}

export interface DataSourceTypeInfo {
  type: DataSourceType;
  defaultPort?: number;
}

const BASE = "/datapaw/data-sources";

/** Normalize snake_case / camelCase backend fields into the frontend record shape. */
function normalizeRecord(raw: Record<string, unknown>): DataSourceRecord {
  return {
    id: String(raw.id ?? ""),
    type: raw.type as DataSourceRecord["type"],
    name: String(raw.name ?? ""),
    config: (raw.config as DataSourceRecord["config"]) ?? {},
    createdAt: String(raw.createdAt ?? raw.created_at ?? ""),
    updatedAt: String(raw.updatedAt ?? raw.updated_at ?? ""),
  };
}

/** Accept either an array body or `{ items }` response from the data source API. */
function normalizeListResponse(raw: unknown): DataSourceListResponse {
  if (Array.isArray(raw)) {
    return {
      items: raw.map((item) =>
        normalizeRecord(item as Record<string, unknown>),
      ),
    };
  }
  const body = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(body.items) ? body.items : [];
  return {
    items: items.map((item) =>
      normalizeRecord(item as Record<string, unknown>),
    ),
  };
}

/** POST /test always returns 200 with { success, message, latencyMs }. */
function normalizeTestResult(raw: unknown): DataSourceTestResult {
  const body = (raw ?? {}) as Record<string, unknown>;
  const latency = body.latencyMs ?? body.latency_ms;
  return {
    success: Boolean(body.success),
    message: String(body.message ?? ""),
    latencyMs:
      latency === undefined || latency === null ? undefined : Number(latency),
  };
}

/** Data source connection REST API. */
export const httpDataSourceApi = {
  /** List all saved data source connections for the active user/agent. */
  list: async () => normalizeListResponse(await request(BASE)),

  /** Create and persist one data source connection. */
  create: async (payload: DataSourceCreatePayload) =>
    normalizeRecord(
      (await request(BASE, {
        method: "POST",
        body: JSON.stringify(payload),
      })) as Record<string, unknown>,
    ),

  /** Test connection settings without persisting them. */
  testConnection: async (payload: DataSourceTestPayload) =>
    normalizeTestResult(
      await request(`${BASE}/test`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    ),

  /** Delete one saved data source connection. */
  remove: (id: string) =>
    request<void>(`${BASE}/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};

export const dataSourceApi = httpDataSourceApi;
