import { request } from "../request";
import type { ApiResponse, PaginationMeta, ApiGraphNode, ApiGraphEdge } from "./types";

/**
 * Send a CMGraph API request and unwrap the response envelope.
 * Automatically prepends /v1/admin prefix to the path.
 * Throws an Error when ok=false.
 */
export async function cmRequest<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await request<ApiResponse<T>>(`/v1/admin${path}`, options);
  if (!response.ok) {
    throw new Error(response.error?.message || "Request failed");
  }
  return response.data as T;
}

/**
 * Send a CMGraph API request and return both data and pagination meta.
 * Useful for list endpoints that return paginated results.
 */
export async function cmRequestWithMeta<T>(
  path: string,
  options?: RequestInit,
): Promise<{ data: T; meta: PaginationMeta | null }> {
  const response = await request<ApiResponse<T>>(`/v1/admin${path}`, options);
  if (!response.ok) {
    throw new Error(response.error?.message || "Request failed");
  }
  return { data: response.data as T, meta: response.meta };
}

/**
 * URL-encode a graph node key (handles colons and other special characters).
 * Example: "met:acme:WebApp:DAU" → "met%3Aacme%3AWebApp%3ADAU"
 */
export function encodeKey(key: string): string {
  return encodeURIComponent(key);
}

/**
 * Build a query string from a params object.
 * Automatically filters out undefined, null, and empty string values.
 */
export function buildPageQuery(params: Record<string, unknown>): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") {
      query.set(k, String(v));
    }
  });
  return query.toString();
}

/**
 * Convert an API graph node to the page-level GraphNode format.
 * Handles two API response formats:
 * - Cypher API (documented): { key, label (type), zone, display_name, properties }
 * - Explorer API (actual):   { id (=key), label (=display_name), group (=type) }
 */
export function apiNodeToGraphNode(apiNode: ApiGraphNode): {
  id: string;
  label: string;
  type: string;
  properties: Record<string, unknown>;
} {
  // Detect actual API format by presence of 'group' field
  const isExplorerFormat = !!apiNode.group;

  const id = (isExplorerFormat ? (apiNode.id ?? apiNode.key) : (apiNode.key ?? apiNode.id)) || "";
  const display_name = isExplorerFormat
    ? apiNode.label                       // actual: label = display_name (e.g. "阿里巴巴")
    : (apiNode.display_name || apiNode.label || id);  // documented
  const type = isExplorerFormat
    ? apiNode.group!                      // actual: group = node type (e.g. "Entity")
    : apiNode.label;                      // documented: label = node type
  const properties = apiNode.properties ?? {};

  return {
    id,
    label: display_name,
    type,
    properties,
  };
}

/**
 * Convert an API graph edge to the page-level GraphEdge format.
 * Handles two API response formats:
 * - Cypher API (documented): { source_key, target_key, rel_type, properties }
 * - Explorer API (actual):   { from, to, type, props }
 */
export function apiEdgeToGraphEdge(apiEdge: ApiGraphEdge): {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, unknown>;
} {
  const source = apiEdge.source_key || apiEdge.from || "";
  const target = apiEdge.target_key || apiEdge.to || "";
  const relType = apiEdge.rel_type || apiEdge.type || "";
  const properties = apiEdge.properties || apiEdge.props || {};

  return {
    id: `${source}-${relType}-${target}`,
    source,
    target,
    type: relType,
    properties,
  };
}

/**
 * When the Cypher response returns table format (graph is null),
 * attempt to extract graph nodes from the table rows.
 * Each row's column value that is an object is treated as a potential node.
 */
export function extractNodesFromRows(
  rows: Record<string, unknown>[],
  columns: string[],
): { id: string; label: string; type: string; properties: Record<string, unknown> }[] {
  const nodes: { id: string; label: string; type: string; properties: Record<string, unknown> }[] = [];
  const seenKeys = new Set<string>();

  rows.forEach((row, rowIndex) => {
    for (const col of columns) {
      const value = row[col];
      if (!value || typeof value !== "object" || Array.isArray(value)) continue;

      const props = value as Record<string, unknown>;

      // Generate node ID — try common key fields, fall back to column-index
      const rawKey = (props.key as string) ||
                     (props._id as string) ||
                     (props.id as string) ||
                     `${col}-${rowIndex}`;
      const id = seenKeys.has(rawKey) ? `${rawKey}-${rowIndex}` : rawKey;
      seenKeys.add(id);

      // Infer node type — try Neo4j metadata fields, then zone, then default
      const type = (props._labels as string) ||
                   (props.label as string) ||
                   (props.type as string) ||
                   (props.zone as string) ||
                   "Node";

      // Infer display name — try common name fields
      const label = (props.display_name as string) ||
                    (props.goal as string) ||
                    (props.name as string) ||
                    (props.title as string) ||
                    (props.canonical_name as string) ||
                    rawKey;

      nodes.push({
        id,
        label: String(label),
        type: String(type),
        properties: props,
      });
    }
  });

  return nodes;
}
