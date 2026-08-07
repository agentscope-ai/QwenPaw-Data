import type { GraphNode, GraphEdge, QueryResult } from "../types";

/** Build rows + columns from a set of graph nodes.
 *  Generates table/text view data scoped to the given nodes only —
 *  each connected component gets its own rows/columns so that table
 *  views don't display nodes from other components. */
export function buildRowsFromNodes(nodes: GraphNode[]): {
  rows: Record<string, unknown>[];
  columns: string[];
} {
  const rows = nodes.map((n) => ({
    key: n.id,
    label: n.type,
    display_name: n.label,
    ...n.properties,
  }));
  const propKeys = new Set<string>();
  nodes.forEach((n) => {
    Object.keys(n.properties ?? {}).forEach((k) => propKeys.add(k));
  });
  const columns = ["key", "label", "display_name", ...propKeys];
  return { rows, columns };
}

/**
 * Return all nodes and edges as a single result group.
 *
 * A single Cypher query's results are always rendered in one unified frame
 * (no splitting by connected components).
 *
 * @returns Array with a single `{ nodes, edges }` entry.
 */
export function splitGraphIntoComponents(
  nodes: GraphNode[],
  edges: GraphEdge[],
): QueryResult[] {
  return [{ nodes, edges }];
}
