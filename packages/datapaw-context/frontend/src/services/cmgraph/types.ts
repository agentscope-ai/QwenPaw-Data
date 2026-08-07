// ============================================================
// CMGraph API — Unified Type Definitions
// Based on api.md specification
// ============================================================

// ------------------------------------------------------------
// 1. Common Types
// ------------------------------------------------------------

/** Unified API response envelope */
export interface ApiResponse<T> {
  ok: boolean;
  data: T | null;
  error: ApiError | null;
  meta: PaginationMeta | null;
}

/** Pagination metadata */
export interface PaginationMeta {
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

/** API error structure */
export interface ApiError {
  code: ApiErrorCode;
  message: string;
  detail?: Record<string, unknown>;
}

/** Unified error codes */
export type ApiErrorCode =
  | "VALIDATION_ERROR"
  | "INVALID_KEY_FORMAT"
  | "WRITE_BLOCKED"
  | "NOT_FOUND"
  | "CONFLICT_DETECTED"
  | "DEPENDENCY_EXISTS"
  | "INTERNAL_ERROR";

// ------------------------------------------------------------
// 2. Common Graph Models (Section 7)
// ------------------------------------------------------------

/** Zone enum — which graph layer a node belongs to */
export type Zone = "metadata" | "trace" | "knowledge" | "_shared" | "cross";

/** Universal graph node model.
 *  Supports two API formats:
 *  - Cypher API (documented): { key, label (type), zone, display_name, properties }
 *  - Explorer API (actual):   { id (=key), label (=display_name), group (=type) } */
export interface ApiGraphNode {
  // Documented API fields
  key?: string;
  label: string;         // Documented: node type (e.g. "Metric"); Actual: display name (e.g. "阿里巴巴")
  zone?: Zone;
  display_name?: string;
  properties?: Record<string, unknown>;
  // Actual API fields (Explorer endpoints use alternative names)
  id?: string;            // Alias for key
  group?: string;         // Alias for label (node type)
}

/** Universal graph edge model.
 *  Supports two API formats:
 *  - Cypher API (documented): { source_key, target_key, rel_type, properties }
 *  - Explorer API (actual):   { from, to, type, props } */
export interface ApiGraphEdge {
  // Documented API fields
  source_key?: string;
  target_key?: string;
  rel_type?: string;
  properties?: Record<string, unknown>;
  is_cross_graph?: boolean;
  // Actual API fields (Explorer endpoints use alternative names)
  from?: string;         // Alias for source_key
  to?: string;           // Alias for target_key
  type?: string;         // Alias for rel_type
  props?: Record<string, unknown>;  // Alias for properties
}

/** Graph data — nodes + edges bundle */
export interface GraphData {
  nodes: ApiGraphNode[];
  edges: ApiGraphEdge[];
}

// ------------------------------------------------------------
// 3. Cypher Module Types
// ------------------------------------------------------------

/** POST /api/v1/admin/cypher — request body */
export interface CypherRequest {
  cypher: string;
  params?: Record<string, unknown>;
  limit?: number;
  response_format?: "auto" | "graph" | "table";
  datasource_id?: string;
}

/** Cypher query summary info */
export interface CypherSummary {
  result_type: string;
  node_count: number;
  edge_count: number;
  labels_hit: string[];
  elapsed_ms: number;
  truncated: boolean;
}

/** POST /api/v1/admin/cypher — response data */
export interface CypherResponse {
  rows: Record<string, unknown>[];
  count: number;
  truncated: boolean;
  columns: string[];
  graph: GraphData | null;
  summary: CypherSummary;
}

// ------------------------------------------------------------
// 4. MG (Metadata Graph) Module Types
// ------------------------------------------------------------

/** Domain list item / detail base */
export interface Domain {
  name: string;
  display_name: string;
  description: string;
  aliases: string[];
  datasource_id: string;
}

/** Domain detail with stats */
export interface DomainDetail extends Domain {
  stats: {
    metric_count: number;
    dimension_count: number;
    dataset_count: number;
  };
}

/** GET /api/v1/admin/mg/domains — query params */
export interface DomainListParams {
  datasource_id?: string;
  page?: number;
  page_size?: number;
}

/** Metric summary in list */
export interface MetricSummary {
  metric_name: string;
  description: string;
  aliases: string[];
  tags: string[];
  role: string;
}

/** Metric formula entry */
export interface MetricFormula {
  dataset: string;
  formula: string;
  formula_evidence: string;
  date_range: string;
}

/** Metric dimension binding */
export interface MetricDimensionBinding {
  dimension_name: string;
  is_display_dimension: boolean;
  is_contribution_dimension: boolean;
}

/** Metric detail */
export interface MetricDetail {
  metric_name: string;
  domain: string;
  description: string;
  unit: string;
  aliases: string[];
  tags: string[];
  role: string;
  formulas: MetricFormula[];
  dimensions: MetricDimensionBinding[];
  anomaly_rules: unknown[];
}

/** GET /api/v1/admin/mg/metrics — query params */
export interface MetricListParams {
  domain?: string;
  datasource_id?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

/** Dimension detail */
export interface DimensionDetail {
  dimension_name: string;
  domain: string;
  dimension_type: string;
  data_type: string;
  aliases: string[];
  description: string;
  hierarchy: {
    parent: string[];
    children: string[];
  };
  values: string[];
  values_total: number;
  bound_metrics: string[];
}

/** GET /api/v1/admin/mg/dimensions — query params */
export interface DimensionListParams {
  domain?: string;
  datasource_id?: string;
  page?: number;
  page_size?: number;
}

/** Dataset column schema */
export interface DatasetColumn {
  column_name: string;
  column_type: string;
  data_type: string;
  description: string;
  sample_values: string[];
}

/** Dataset detail with schema */
export interface DatasetSchema {
  dataset_name: string;
  domain: string;
  description: string;
  table_name: string;
  columns: DatasetColumn[];
}

/** GET /api/v1/admin/mg/datasets — query params */
export interface DatasetListParams {
  domain?: string;
  datasource_id?: string;
  page?: number;
  page_size?: number;
}

/** MG edge item (from nodes/{key}/edges) */
export interface MgEdgeItem {
  rel_type: string;
  direction: "in" | "out" | "both";
  source_key: string;
  target_key: string;
  target_label: string;
  target_display: string;
  properties: Record<string, unknown>;
}

/** GET /api/v1/admin/mg/nodes/{key}/edges — query params */
export interface MgNodeEdgesParams {
  direction?: "in" | "out" | "both";
  rel_type?: string;
  page?: number;
  page_size?: number;
}

/** GET /api/v1/admin/mg/nodes/{key}/edges — response data */
export interface MgNodeEdgesResponse {
  edges: MgEdgeItem[];
}

// ------------------------------------------------------------
// 5. TG (Trace Graph) Module Types
// ------------------------------------------------------------

/** Task status enum */
export type TaskStatus =
  | "success"
  | "failed"
  | "running"
  | "archived"
  | "invalidated";

/** Task list item */
export interface TaskListItem {
  key: string;
  goal: string;
  status: TaskStatus;
  task_signature: string;
  created_at: string;
  step_count: number;
  claim_count: number;
}

/** GET /api/v1/admin/tg/tasks — query params */
export interface TaskListParams {
  status?: TaskStatus;
  date_from?: string;
  date_to?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

/** PATCH /api/v1/admin/tg/tasks/{key}/status — body */
export interface TaskStatusUpdatePayload {
  status: "archived" | "invalidated";
  reason?: string;
}

/** POST /api/v1/admin/tg/tasks/batch-archive — body */
export interface TaskBatchArchivePayload {
  task_keys: string[];
  reason?: string;
}

/** POST /api/v1/admin/tg/tasks/batch-delete — body */
export interface TaskBatchDeletePayload {
  task_keys: string[];
}

/** Claim subject type enum */
export type ClaimSubjectType =
  | "metric_mapping"
  | "join_path"
  | "caliber_rule"
  | "time_parsing"
  | "general";

/** Claim list item */
export interface ClaimListItem {
  key: string;
  text: string;
  confidence: number;
  subject_type: ClaimSubjectType;
  predicate: string;
  object: string;
  task_key: string;
  valid_at: string;
  valid_to: string | null;
}

/** GET /api/v1/admin/tg/claims — query params */
export interface ClaimListParams {
  subject_type?: ClaimSubjectType;
  q?: string;
  valid?: boolean;
  task_key?: string;
  page?: number;
  page_size?: number;
}

/** PATCH /api/v1/admin/tg/claims/{key} — body */
export interface ClaimUpdatePayload {
  text?: string;
  confidence?: number;
  subject_type?: ClaimSubjectType;
  predicate?: string;
  object?: string;
}

/** POST /api/v1/admin/tg/claims/{key}/invalidate — body */
export interface ClaimInvalidatePayload {
  reason: string;
}

/** Strategy card polarity */
export type StrategyPolarity = "positive" | "negative";

/** Strategy card memory tier */
export type MemoryTier = "hot" | "warm" | "cold";

/** Strategy card list item */
export interface StrategyCard {
  key: string;
  task_signature: string;
  polarity: StrategyPolarity;
  memory_tier: MemoryTier;
  hit_count: number;
  success_rate: number;
  strategy_semantics: string;
  example_query: string;
  valid_at: string;
  last_hit_at: string;
}

/** GET /api/v1/admin/tg/strategies — query params */
export interface StrategyListParams {
  polarity?: StrategyPolarity;
  memory_tier?: MemoryTier;
  page?: number;
  page_size?: number;
}

/** PATCH /api/v1/admin/tg/strategies/{key} — body */
export interface StrategyUpdatePayload {
  strategy_semantics?: string;
  memory_tier?: MemoryTier;
  source_trust?: number;
  polarity?: StrategyPolarity;
  example_query?: string;
}

/** POST /api/v1/admin/tg/strategies/{key}/invalidate — body */
export interface StrategyInvalidatePayload {
  reason: string;
}

/** Tag list item */
export interface TagListItem {
  key: string;
  name: string;
  category: string;
  tagged_task_count: number;
}

/** GET /api/v1/admin/tg/tags — query params */
export interface TagListParams {
  category?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

// ------------------------------------------------------------
// 6. KG (Knowledge Graph) Module Types
// ------------------------------------------------------------

/** Entity lifecycle state */
export type LifecycleState = "active" | "deprecated" | "merged";

/** Entity list item */
export interface EntityListItem {
  key: string;
  display_name: string;
  type: string;
  lifecycle_state: LifecycleState;
  zone: Zone;
}

/** GET /api/v1/admin/kg/entities — query params */
export interface EntityListParams {
  q?: string;
  type?: string;
  lifecycle_state?: LifecycleState;
  page?: number;
  page_size?: number;
}

/** Entity neighbor info */
export interface EntityNeighbor {
  rel_type: string;
  direction: "in" | "out";
  other_key: string;
  other_label: string;
  other_name: string;
  rel_props: Record<string, unknown>;
}

/** Entity detail */
export interface EntityDetail {
  entity: {
    key: string;
    canonical_name: string;
    type: string;
    aliases: string[];
    lifecycle_state: LifecycleState;
    description: string;
  };
  neighbors: EntityNeighbor[];
}

/** PUT /api/v1/admin/kg/entities/{key} — body */
export interface EntityCreatePayload {
  canonical_name: string;
  type: string;
  aliases?: string[];
  description?: string;
  lifecycle_state?: LifecycleState;
}

/** Alias for update (same structure) */
export type EntityUpdatePayload = EntityCreatePayload;

/** PUT /api/v1/admin/kg/events/{key} — body */
export interface EventCreatePayload {
  name: string;
  type: string;
  description?: string;
  date_from?: string;
  date_to?: string;
  scope?: string;
  zone?: Zone;
  source_id?: string;
  source_trust?: number;
  extractor?: string;
}

/** Alias for update (same structure) */
export type EventUpdatePayload = EventCreatePayload;

/** POST /api/v1/admin/kg/edges/related-to — body */
export interface EdgeRelatedToPayload {
  from_key: string;
  to_key: string;
  relation_subtype: string;
  description?: string;
}

/** POST /api/v1/admin/kg/edges/about — body */
export interface EdgeAboutPayload {
  event_key: string;
  entity_key: string;
  connect: boolean;
}

/** POST /api/v1/admin/kg/edges/cross-graph — body */
export interface EdgeCrossGraphPayload {
  from_key: string;
  to_key: string;
  rel_type: string;
  properties?: Record<string, unknown>;
}

/** DELETE /api/v1/admin/kg/edges/cross-graph — body */
export interface EdgeCrossGraphDeletePayload {
  from_key: string;
  to_key: string;
  rel_type: string;
}

/** PATCH /api/v1/admin/kg/edges/properties — body */
export interface EdgePropertiesPayload {
  from_key: string;
  to_key: string;
  rel_type: string;
  properties: Record<string, unknown>;
}

/** DELETE /api/v1/admin/kg/edges/adjacent — body */
export interface EdgeAdjacentDeletePayload {
  anchor_key: string;
  other_key: string;
  rel_type: string;
  direction: "in" | "out";
}

/** DELETE /api/v1/admin/kg/edges/by-type — body */
export interface EdgeByTypeDeletePayload {
  anchor_key: string;
  rel_type: string;
  direction_scope: "in" | "out" | "both";
}

/** POST /api/v1/admin/kg/entities/batch-delete — body */
export interface EntityBatchDeletePayload {
  keys: string[];
}

/** Relationship type info */
export interface RelTypeInfo {
  rel_types: string[];
}

// ------------------------------------------------------------
// 7. Explorer Module Types
// ------------------------------------------------------------

/** Zone mode for global graph query */
export type ZoneMode = "all" | "metadata" | "trace" | "knowledge";

/** POST /api/v1/admin/explorer/global-graph — body */
export interface GlobalGraphParams {
  max_edges?: number;
  max_nodes?: number;
  domain_roots_only?: boolean;
  skeleton?: boolean;
  zone_mode?: ZoneMode;
  task_roots?: boolean;
  max_task_roots?: number;
  datasource_id?: string;
}

/** POST /api/v1/admin/explorer/domain-graph — body */
export interface DomainGraphParams {
  domain: string;
  datasource_id: string;
}

/** POST /api/v1/admin/explorer/expand-node — body */
export interface ExpandNodeParams {
  key: string;
  limit?: number;
  zone?: Zone | null;
  label?: string | null;
}

/** POST /api/v1/admin/explorer/expand-layer — body */
export interface ExpandLayerParams {
  key: string;
  direction: "in" | "out";
  limit?: number;
}

/** POST /api/v1/admin/explorer/search-nodes — body */
export interface SearchNodesParams {
  query: string;
  limit?: number;
}

/** Match mode for subgraph search */
export type MatchMode = "exact" | "fuzzy";

/** POST /api/v1/admin/explorer/search-subgraph — body */
export interface SearchSubgraphParams {
  query: string;
  scope?: Zone[];
  match_mode?: MatchMode;
  hops?: number;
  limit?: number;
}

/** POST /api/v1/admin/explorer/search-subgraph — response data */
export interface SearchSubgraphResponse {
  hit_nodes: ApiGraphNode[];
  nodes: ApiGraphNode[];
  edges: ApiGraphEdge[];
}

/** Schema node label info */
export interface SchemaNodeLabel {
  label: string;
  count: number;
  zone: Zone;
}

/** Schema relationship type info */
export interface SchemaRelType {
  type: string;
  count: number;
  zone: Zone;
  source_zone: Zone;
  target_zone: Zone;
}

/** GET /api/v1/admin/explorer/schema — response data */
export interface SchemaInfo {
  node_labels: SchemaNodeLabel[];
  relationship_types: SchemaRelType[];
}

/** GET /api/v1/admin/explorer/nodes/{key} — response data */
export interface NodeDetail {
  key: string;
  labels: string[];
  zone: Zone;
  properties: Record<string, unknown>;
  editable_fields: string[];
}

/** Cross-graph neighbor item */
export interface CrossGraphNeighbor {
  rel_type: string;
  direction: "in" | "out";
  other_key: string;
  other_label: string;
  other_name: string;
  other_zone: Zone;
}

/** POST /api/v1/admin/explorer/edge-detail — body */
export interface EdgeDetailParams {
  source_key: string;
  target_key: string;
  rel_type: string;
}

/** POST /api/v1/admin/explorer/edge-detail — response data */
export interface EdgeDetail {
  source_key: string;
  target_key: string;
  rel_type: string;
  source_label: string;
  target_label: string;
  source_zone: Zone;
  target_zone: Zone;
  properties: Record<string, unknown>;
  editable_fields: string[];
  is_cross_graph: boolean;
}
