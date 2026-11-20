/** Adapter: real scoped backend payloads → GraphCenter dataset.
 *  The explorer API returns { id, label, group, props } nodes and
 *  { from, to, type, props } edges; the user-graph API provides UG users and
 *  memories. This module folds both into the four-layer GraphDataset shape
 *  the pages render. */
import type { ApiGraphEdge, ApiGraphNode } from '@/services/cmgraph/types';
import type { UserGraphMemory, UserGraphUser } from '@/services/userGraph/types';
import type { GraphDataset, GraphEdge, GraphNode, LayerId, NodeType } from '../types';

/** Neo4j label → GraphCenter node type. Labels without an entry fall back to
 *  a generic per-layer type so nothing visible is dropped. */
const LABEL_TO_TYPE: Record<string, NodeType> = {
  Domain: 'domain',
  DataSource: 'datasource',
  Dataset: 'dataset',
  Table: 'table',
  Column: 'column',
  DatasetColumn: 'column',
  // W3 拓扑（14→9）：DatasetColumn→DatasetField、Formula→MetricCaliber、
  // 新增 VerifiedQuery。旧名保留兼容历史图。
  DatasetField: 'datasetfield',
  MetricCaliber: 'metriccaliber',
  VerifiedQuery: 'verifiedquery',
  Operator: 'operator',
  Metric: 'metric',
  Dimension: 'dimension',
  DimensionValue: 'dimension',
  Formula: 'formula',
  Caliber: 'caliber',
  Task: 'task',
  Step: 'step',
  ToolCall: 'toolcall',
  Session: 'session',
  Claim: 'claim',
  Strategy: 'strategy',
  StrategyCard: 'strategy',
  Experience: 'experience',
  Turn: 'turn',
  Entity: 'entity',
  Event: 'event',
  User: 'user',
  Memory: 'memory',
  UserMemory: 'memory',
};

const MG_LABELS = new Set([
  'Domain',
  'DataSource',
  'Dataset',
  'DatasetColumn',
  'DatasetField',
  'Table',
  'Column',
  'Metric',
  'Dimension',
  'DimensionValue',
  'Formula',
  'Caliber',
  'MetricCaliber',
  'VerifiedQuery',
  'Database',
  'Schema',
  'Operator',
]);
const TG_LABELS = new Set([
  'Task',
  'Step',
  'ToolCall',
  'Session',
  'Claim',
  'Strategy',
  'StrategyCard',
  'Experience',
  'Turn',
]);
const KG_LABELS = new Set(['Entity', 'Event']);
const UG_LABELS = new Set(['User', 'Memory', 'UserMemory']);

function layerForLabel(label: string): LayerId {
  if (MG_LABELS.has(label)) return 'MG';
  if (TG_LABELS.has(label)) return 'TG';
  if (KG_LABELS.has(label)) return 'KG';
  if (UG_LABELS.has(label)) return 'UG';
  return 'MG';
}

/** Display description picked from the props a label actually carries. */
function pickDesc(label: string, props: Record<string, unknown>): string | undefined {
  const candidates: unknown[] = [];
  if (label === 'Task') candidates.push(props.goal);
  if (label === 'Claim') candidates.push(props.text);
  if (label === 'Strategy' || label === 'StrategyCard') candidates.push(props.strategy_semantics);
  if (label === 'Event') candidates.push(props.description);
  candidates.push(props.description);
  const hit = candidates.find((v) => typeof v === 'string' && v.trim());
  return typeof hit === 'string' ? hit : undefined;
}

export function apiNodeToGraphNode(node: ApiGraphNode): GraphNode {
  const label = node.group || node.label || 'Node';
  const props = (node.properties ?? node.props ?? {}) as Record<string, unknown>;
  const id = String(node.id ?? node.key ?? '');
  const display = String(
    (node.label && node.group ? node.label : props.display_name || props.name || '') || id,
  );
  const str = (v: unknown) => (typeof v === 'string' && v.trim() ? v : undefined);
  return {
    id,
    label: display,
    type: LABEL_TO_TYPE[label] || (layerForLabel(label) === 'MG' ? 'table' : 'entity'),
    layer: layerForLabel(label),
    domain: str(props.domain),
    datasource: str(props.datasource_id),
    definition: label === 'Metric' ? str(props.description) : undefined,
    desc: pickDesc(label, props),
    props,
  };
}

export function apiEdgeToGraphEdge(edge: ApiGraphEdge): GraphEdge {
  return {
    source: String(edge.source_key ?? edge.from ?? ''),
    target: String(edge.target_key ?? edge.to ?? ''),
    relation: String(edge.rel_type ?? edge.type ?? ''),
  };
}

/** Convert one explorer payload (nodes + edges) into dataset entries. */
export function datasetFromApiGraph(
  apiNodes: ApiGraphNode[],
  apiEdges: ApiGraphEdge[],
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes = apiNodes.map(apiNodeToGraphNode).filter((n) => n.id);
  const ids = new Set(nodes.map((n) => n.id));
  const edges = apiEdges
    .map(apiEdgeToGraphEdge)
    .filter((e) => e.source && e.target && ids.has(e.source) && ids.has(e.target));
  return { nodes, edges };
}

const MEMORY_KIND_LABEL: Record<string, string> = {
  principle: '原则',
  preference: '偏好',
  anchored_rule: '锚定规则',
  trigger: '触发器',
};

/** Synthesize UG layer nodes/edges from the user-graph API. The explorer
 *  snapshot does not cover User/Memory, so UG is folded in separately.
 *  ``names`` maps user ids to display names (auth usernames win over the UG
 *  profile display name; the id prefix stays as the last resort). */
export function datasetFromUserGraph(
  users: UserGraphUser[],
  memoriesByUser: Record<string, UserGraphMemory[]>,
  names: Record<string, string> = {},
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  users.forEach((u) => {
    const uid = `ug:user:${u.user_id}`;
    const display =
      names[u.user_id] || u.profile?.display_name || `用户·${u.user_id.slice(0, 10)}…`;
    nodes.push({
      id: uid,
      label: display,
      type: 'user',
      layer: 'UG',
      desc: u.profile?.description ?? undefined,
    });
    (memoriesByUser[u.user_id] || []).forEach((m) => {
      const mid = `ug:memory:${m.id}`;
      nodes.push({
        id: mid,
        label: m.description,
        type: 'memory',
        layer: 'UG',
        memoryKind: MEMORY_KIND_LABEL[m.type] || m.type,
        desc: m.description,
      });
      edges.push({ source: uid, target: mid, relation: 'HAS' });
    });
  });
  return { nodes, edges };
}

/** Merge supported CM layers, dropping retired TG nodes and dangling edges. */
export function mergeDatasets(
  workspace: string,
  source: string,
  parts: { nodes: GraphNode[]; edges: GraphEdge[] }[],
): GraphDataset {
  const seen = new Set<string>();
  const nodes: GraphNode[] = [];
  parts.forEach((p) =>
    p.nodes.forEach((n) => {
      if (n.layer !== 'TG' && !seen.has(n.id)) {
        seen.add(n.id);
        nodes.push(n);
      }
    }),
  );
  const edgeSeen = new Set<string>();
  const edges: GraphEdge[] = [];
  parts.forEach((p) =>
    p.edges.forEach((e) => {
      if (!seen.has(e.source) || !seen.has(e.target)) return;
      const k = `${e.source}>${e.target}:${e.relation}`;
      if (edgeSeen.has(k)) return;
      edgeSeen.add(k);
      edges.push(e);
    }),
  );
  const byLayer: Record<LayerId, number> = { MG: 0, TG: 0, KG: 0, UG: 0 };
  nodes.forEach((n) => {
    byLayer[n.layer] += 1;
  });
  return {
    nodes,
    edges,
    meta: { workspace, source, nodeCount: nodes.length, edgeCount: edges.length, byLayer },
  };
}
