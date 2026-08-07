// ============================================================
// CMGraph API — Explorer Module (Graph Exploration)
// Based on api.md Section 6: /api/v1/admin/explorer/*
// ============================================================

import { cmRequest, encodeKey, buildPageQuery } from "./helpers";
import type {
  GlobalGraphParams,
  DomainGraphParams,
  ExpandNodeParams,
  ExpandLayerParams,
  SearchNodesParams,
  SearchSubgraphParams,
  SearchSubgraphResponse,
  SchemaInfo,
  NodeDetail,
  CrossGraphNeighbor,
  EdgeDetailParams,
  EdgeDetail,
  GraphData,
  ApiGraphNode,
} from "./types";

// ------------------------------------------------------------
// Helper: build POST request options with JSON body
// ------------------------------------------------------------
function postJson(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// ------------------------------------------------------------
// 6.1 Global Graph
// POST /api/v1/admin/explorer/global-graph
// ------------------------------------------------------------
async function getGlobalGraph(params: GlobalGraphParams = {}): Promise<GraphData> {
  return cmRequest<GraphData>("/explorer/global-graph", postJson(params));
}

// ------------------------------------------------------------
// 6.2 Domain Sub-graph
// POST /api/v1/admin/explorer/domain-graph
// ------------------------------------------------------------
async function getDomainGraph(params: DomainGraphParams): Promise<GraphData> {
  return cmRequest<GraphData>("/explorer/domain-graph", postJson(params));
}

// ------------------------------------------------------------
// 6.3 Single Node Neighbor Expand
// POST /api/v1/admin/explorer/expand-node
// ------------------------------------------------------------
async function expandNode(params: ExpandNodeParams): Promise<GraphData> {
  return cmRequest<GraphData>("/explorer/expand-node", postJson(params));
}

// ------------------------------------------------------------
// 6.4 Directional Layer Expand
// POST /api/v1/admin/explorer/expand-layer
// ------------------------------------------------------------
async function expandLayer(params: ExpandLayerParams): Promise<GraphData> {
  return cmRequest<GraphData>("/explorer/expand-layer", postJson(params));
}

// ------------------------------------------------------------
// 6.5 Basic Node Search
// POST /api/v1/admin/explorer/search-nodes
// ------------------------------------------------------------
async function searchNodes(params: SearchNodesParams): Promise<ApiGraphNode[]> {
  return cmRequest<ApiGraphNode[]>("/explorer/search-nodes", postJson(params));
}

// ------------------------------------------------------------
// 6.6 Subgraph Search (n-hop expansion)
// POST /api/v1/admin/explorer/search-subgraph
// ------------------------------------------------------------
async function searchSubgraph(params: SearchSubgraphParams): Promise<SearchSubgraphResponse> {
  return cmRequest<SearchSubgraphResponse>("/explorer/search-subgraph", postJson(params));
}

// ------------------------------------------------------------
// 6.7 Schema (Node Labels + Relationship Types)
// GET /api/v1/admin/explorer/schema
// ------------------------------------------------------------
async function getSchema(): Promise<SchemaInfo> {
  return cmRequest<SchemaInfo>("/explorer/schema");
}

// ------------------------------------------------------------
// 6.8 Generic Node Detail
// GET /api/v1/admin/explorer/nodes/{node_key}
// ------------------------------------------------------------
async function getNodeDetail(key: string): Promise<NodeDetail> {
  return cmRequest<NodeDetail>(`/explorer/nodes/${encodeKey(key)}`);
}

// ------------------------------------------------------------
// 6.9 Cross-Graph Neighbors
// GET /api/v1/admin/explorer/nodes/{node_key}/cross-graph?limit=50
// ------------------------------------------------------------
async function getCrossGraphNeighbors(
  key: string,
  params: { limit?: number } = {},
): Promise<CrossGraphNeighbor[]> {
  const qs = buildPageQuery(params);
  const path = `/explorer/nodes/${encodeKey(key)}/cross-graph${qs ? `?${qs}` : ""}`;
  return cmRequest<CrossGraphNeighbor[]>(path);
}

// ------------------------------------------------------------
// 6.10 Edge Detail
// POST /api/v1/admin/explorer/edge-detail
// ------------------------------------------------------------
async function getEdgeDetail(params: EdgeDetailParams): Promise<EdgeDetail> {
  return cmRequest<EdgeDetail>("/explorer/edge-detail", postJson(params));
}

// ------------------------------------------------------------
// 6.11 List Nodes (convenience wrapper over global-graph)
// Returns first N nodes from the global graph
// ------------------------------------------------------------
async function listNodes(params: { limit?: number } = {}): Promise<ApiGraphNode[]> {
  const { nodes } = await getGlobalGraph({ max_nodes: params.limit ?? 50 });
  return nodes;
}

// ------------------------------------------------------------
// Public API
// ------------------------------------------------------------
export const explorerApi = {
  getGlobalGraph,
  getDomainGraph,
  expandNode,
  expandLayer,
  searchNodes,
  searchSubgraph,
  getSchema,
  getNodeDetail,
  getCrossGraphNeighbors,
  getEdgeDetail,
  listNodes,
};
