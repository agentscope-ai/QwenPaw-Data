import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { mergeDatasets } from '../../pages/GraphCenter/dataset/apiAdapter';
import { createGraphDb, LAYERS } from '../../pages/GraphCenter/dataset/graphDb';
import { demoDataset } from '../../pages/GraphCenter/dataset/graphData';
import type { ExpandNodeParams, GlobalGraphParams, SearchSubgraphParams } from './types';

// These are compile-time contracts: retired request values must be rejected by tsc.
const global: GlobalGraphParams = { zone_mode: 'knowledge', owner_id: 'member' };
const expand: ExpandNodeParams = { key: 'entity', zone: 'user' };
const search: SearchSubgraphParams = { query: 'test', scope: ['metadata', 'knowledge', 'user'] };
// @ts-expect-error CM 1.0 removed the trace zone.
const retiredGlobal: GlobalGraphParams = { zone_mode: 'trace' };
// @ts-expect-error CM 1.0 removed usage filtering.
const retiredUsage: GlobalGraphParams = { usage_filter: true };
// @ts-expect-error Response-only cross zones cannot be expansion inputs.
const retiredExpand: ExpandNodeParams = { key: 'entity', zone: 'cross' };
// @ts-expect-error Trace cannot be searched.
const retiredSearch: SearchSubgraphParams = { query: 'test', scope: ['trace'] };
void [global, expand, search, retiredGlobal, retiredUsage, retiredExpand, retiredSearch];

test('CM 1.0 dataset excludes TG and its edges without dropping MG/KG/UG content', () => {
  const nodes = [
    { id: 'metric', label: 'Metric', type: 'metric', layer: 'MG' },
    { id: 'task', label: 'Task', type: 'task', layer: 'TG' },
    { id: 'entity', label: 'Entity', type: 'entity', layer: 'KG' },
    { id: 'memory', label: 'Memory', type: 'memory', layer: 'UG' },
  ] as const;
  const dataset = mergeDatasets('workspace', 'api', [
    {
      nodes: [...nodes],
      edges: [
        { source: 'task', target: 'metric', relation: 'USES' },
        { source: 'metric', target: 'entity', relation: 'REFERS_TO' },
        { source: 'memory', target: 'entity', relation: 'REFERS_TO' },
      ],
    },
  ]);
  assert.deepEqual(
    dataset.nodes.map((node) => node.id),
    ['metric', 'entity', 'memory'],
  );
  assert.equal(dataset.edges.length, 2);
  assert.deepEqual(dataset.meta.byLayer, { MG: 1, TG: 0, KG: 1, UG: 1 });
  assert.equal(createGraphDb(dataset).getNode('task'), null);
  assert.deepEqual(LAYERS, ['MG', 'KG', 'UG']);
});

test('offline demo cannot restore retired trace content', () => {
  const dataset = mergeDatasets('workspace', 'demo', [demoDataset()]);
  const ids = new Set(dataset.nodes.map((node) => node.id));
  assert.ok(dataset.nodes.length > 0);
  assert.ok(dataset.nodes.every((node) => node.layer !== 'TG'));
  assert.ok(dataset.edges.every((edge) => ids.has(edge.source) && ids.has(edge.target)));
  assert.equal(dataset.meta.nodeCount, dataset.nodes.length);
});

test('frontend has no retired CM HTTP paths or removed global-graph request fields', () => {
  const root = new URL('../../', import.meta.url);
  for (const path of readdirSync(root, { recursive: true, encoding: 'utf8' })) {
    if (!/\.(ts|tsx)$/.test(path) || path.endsWith('.test.ts')) continue;
    const source = readFileSync(new URL(path, root), 'utf8');
    assert.doesNotMatch(
      source,
      /["'`]\/(?:api\/)?(?:v1\/(?:admin\/tg\/|traces(?:[/?"'`])|trace\/|cm\/(?:search_context|recall_experience))|v3\/cm\/(?:graph_schema|graph_stats|find_anchors|query_graph|expand_subgraph|validate_experience|recompute_confidence|promote|demote|assemble_guidance|distill|mutate_graph))/,
      path,
    );
    assert.doesNotMatch(source, /\b(?:task_roots|max_task_roots|usage_filter)\s*:/, path);
  }
});
