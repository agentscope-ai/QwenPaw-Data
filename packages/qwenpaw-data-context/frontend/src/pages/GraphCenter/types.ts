/** Graph Center (图谱中心) shared types.
 *  The dataset is fetched from the real scoped backend APIs (explorer +
 *  user-graph) and mapped into this shape; the pages only ever see this
 *  shape, so the data source stays swappable. The UI starts empty; a static
 *  demo snapshot (dataset/graphData.ts) is used only as a clearly-labeled
 *  offline fallback when the backend is unreachable. */

export type LayerId = 'MG' | 'TG' | 'KG' | 'UG';

export type NodeType =
  | 'domain'
  | 'datasource'
  | 'dataset'
  | 'table'
  | 'column'
  | 'datasetfield'
  | 'metriccaliber'
  | 'verifiedquery'
  | 'operator'
  | 'metric'
  | 'dimension'
  | 'formula'
  | 'caliber'
  | 'task'
  | 'step'
  | 'toolcall'
  | 'session'
  | 'claim'
  | 'strategy'
  | 'experience'
  | 'turn'
  | 'entity'
  | 'event'
  | 'memory'
  | 'user';

export interface GraphNode {
  id: string;
  label: string;
  type: NodeType;
  layer: LayerId;
  domain?: string;
  datasource?: string;
  definition?: string;
  desc?: string;
  memoryKind?: string;
  /** Raw backend properties (for tooltips and future detail views). */
  props?: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: string;
}

export interface GraphMeta {
  workspace: string;
  source: string;
  nodeCount: number;
  edgeCount: number;
  byLayer: Record<LayerId, number>;
}

export interface GraphDataset {
  nodes: GraphNode[];
  edges: GraphEdge[];
  meta: GraphMeta;
}

export interface SubgraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface DomainStat {
  id: string;
  label: string;
  metricCount: number;
  dsCount: number;
}

export type HealthIssueCode = 'missingDef' | 'unusedDim' | 'isolated';

export interface HealthIssue {
  id: string;
  label: string;
  type: NodeType;
  layer: LayerId;
  domain: string | null;
  issue: HealthIssueCode;
  refCount: number;
}

export interface HealthSummary {
  missingDef: number;
  unusedDim: number;
  isolated: number;
}

export interface GlobalStats {
  nodeCount: number;
  edgeCount: number;
  byLayer: Record<LayerId, number>;
  meta: GraphMeta;
}
