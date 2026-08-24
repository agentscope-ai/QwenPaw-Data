import { cmRequest, cmRequestWithMeta, encodeKey, buildPageQuery } from "./helpers";
import type {
  Domain,
  DomainDetail,
  DomainListParams,
  MetricSummary,
  MetricDetail,
  MetricListParams,
  DimensionDetail,
  DimensionListParams,
  DatasetSchema,
  DatasetListParams,
  MgNodeEdgesParams,
  MgNodeEdgesResponse,
  PaginationMeta,
} from "./types";

// ------------------------------------------------------------
// MG (Metadata Graph) — Read-only API module
// Prefix: /api/v1/admin/mg
// ------------------------------------------------------------

/** GET /api/v1/admin/mg/domains */
async function listDomains(
  params?: DomainListParams,
): Promise<{ data: Domain[]; meta: PaginationMeta | null }> {
  const qs = params ? buildPageQuery(params as Record<string, unknown>) : "";
  const path = qs ? `/mg/domains?${qs}` : "/mg/domains";
  return cmRequestWithMeta<Domain[]>(path);
}

/** GET /api/v1/admin/mg/domains/:key */
async function getDomain(
  key: string,
  params?: { datasource_id?: string },
): Promise<DomainDetail> {
  const qs = params ? buildPageQuery(params) : "";
  const path = qs
    ? `/mg/domains/${encodeKey(key)}?${qs}`
    : `/mg/domains/${encodeKey(key)}`;
  return cmRequest<DomainDetail>(path);
}

/** GET /api/v1/admin/mg/metrics */
async function listMetrics(
  params?: MetricListParams,
): Promise<{ data: MetricSummary[]; meta: PaginationMeta | null }> {
  const qs = params ? buildPageQuery(params as Record<string, unknown>) : "";
  const path = qs ? `/mg/metrics?${qs}` : "/mg/metrics";
  return cmRequestWithMeta<MetricSummary[]>(path);
}

/** GET /api/v1/admin/mg/metrics/:key */
async function getMetric(
  key: string,
  params?: { domain?: string; datasource_id?: string },
): Promise<MetricDetail> {
  const qs = params ? buildPageQuery(params) : "";
  const path = qs
    ? `/mg/metrics/${encodeKey(key)}?${qs}`
    : `/mg/metrics/${encodeKey(key)}`;
  return cmRequest<MetricDetail>(path);
}

/** GET /api/v1/admin/mg/dimensions */
async function listDimensions(
  params?: DimensionListParams,
): Promise<{ data: DimensionDetail[]; meta: PaginationMeta | null }> {
  const qs = params ? buildPageQuery(params as Record<string, unknown>) : "";
  const path = qs ? `/mg/dimensions?${qs}` : "/mg/dimensions";
  return cmRequestWithMeta<DimensionDetail[]>(path);
}

/** GET /api/v1/admin/mg/dimensions/:key */
async function getDimension(
  key: string,
  params?: { domain?: string; datasource_id?: string },
): Promise<DimensionDetail> {
  const qs = params ? buildPageQuery(params) : "";
  const path = qs
    ? `/mg/dimensions/${encodeKey(key)}?${qs}`
    : `/mg/dimensions/${encodeKey(key)}`;
  return cmRequest<DimensionDetail>(path);
}

/** GET /api/v1/admin/mg/datasets */
async function listDatasets(
  params?: DatasetListParams,
): Promise<{ data: DatasetSchema[]; meta: PaginationMeta | null }> {
  const qs = params ? buildPageQuery(params as Record<string, unknown>) : "";
  const path = qs ? `/mg/datasets?${qs}` : "/mg/datasets";
  return cmRequestWithMeta<DatasetSchema[]>(path);
}

/** GET /api/v1/admin/mg/datasets/:key */
async function getDataset(
  key: string,
  params?: { domain?: string; datasource_id?: string },
): Promise<DatasetSchema> {
  const qs = params ? buildPageQuery(params) : "";
  const path = qs
    ? `/mg/datasets/${encodeKey(key)}?${qs}`
    : `/mg/datasets/${encodeKey(key)}`;
  return cmRequest<DatasetSchema>(path);
}

/** GET /api/v1/admin/mg/nodes/:key/edges */
async function getNodeEdges(
  key: string,
  params?: MgNodeEdgesParams,
): Promise<MgNodeEdgesResponse> {
  const qs = params ? buildPageQuery(params as Record<string, unknown>) : "";
  const path = qs
    ? `/mg/nodes/${encodeKey(key)}/edges?${qs}`
    : `/mg/nodes/${encodeKey(key)}/edges`;
  return cmRequest<MgNodeEdgesResponse>(path);
}

export const mgApi = {
  listDomains,
  getDomain,
  listMetrics,
  getMetric,
  listDimensions,
  getDimension,
  listDatasets,
  getDataset,
  getNodeEdges,
};
