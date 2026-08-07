export interface GraphNode {
  id: string;
  label: string;
  type: string; // Node label type (determines color)
  properties?: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string; // Relationship type (edge label)
  properties?: Record<string, unknown>;
}

export interface QueryResult {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface NodeLabelInfo {
  label: string;
  count: number;
  color: string;
}

export interface RelationshipTypeInfo {
  type: string;
  count: number;
}

export type ViewTab = "graph" | "table" | "text" | "code";

/** Graph zone tab key — KG / TG / MG */
export type GraphZone = "kg" | "tg" | "mg";

/** Zone tab configuration: editable / readonly ranges per layer */
export interface ZoneTabConfig {
  key: GraphZone;
  /** API zone_mode value */
  zoneMode: "knowledge" | "trace" | "metadata";
  /** Node labels whose properties are editable in this zone */
  editableLabels: string[];
  /** Node labels that are read-only in this zone */
  readonlyLabels: string[];
  /** Whether all edges are editable */
  allEdgesEditable: boolean;
}

// Query metadata (for CodeView display)
export interface QueryMeta {
  serverVersion?: string;
  serverAddress?: string;
  query: string;
  queryType?: string;
  counters?: Record<string, number>;
  totalRecords?: number;
  startedAfterMs?: number;
  completedAfterMs?: number;
  labelsHit?: string[];    // Labels hit by the query (from CypherSummary)
  elapsedMs?: number;     // Server-side elapsed time in milliseconds
}

// Query result frame (card)
export interface QueryFrame {
  id: string;
  query: string;          // Executed Cypher query
  subTitle?: string;      // Optional sub-label (e.g. "#1 / 3" when split into multiple cards)
  timestamp: number;      // Execution timestamp
  activeTab: ViewTab;     // Currently active view tab
  result: QueryResult;    // Graph result data (nodes + edges for GraphCanvas)
  rows?: Record<string, unknown>[];   // Raw row data (for TableView/TextView)
  columns?: string[];                  // Column names
  meta?: QueryMeta;                    // Query metadata (for CodeView)
}
