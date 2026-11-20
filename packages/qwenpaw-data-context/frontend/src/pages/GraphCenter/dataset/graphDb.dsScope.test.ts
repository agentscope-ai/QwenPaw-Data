/** Datasource chip narrowing is closure-based: shared semantic nodes stay
 *  visible for every datasource they belong to, while other datasources'
 *  exclusive assets stay out. Regression for the stamp-filter blank-canvas
 *  bug (weave only stamps Dataset nodes). */
import assert from 'node:assert/strict';
import { apiNodeToGraphNode } from './apiAdapter';
import { createGraphDb } from './graphDb';
import type { GraphDataset, GraphEdge, GraphNode } from '../types';

function node(id: string, type: GraphNode['type'], datasource = '', key = ''): GraphNode {
  return { id, label: id, type, layer: 'MG', datasource, props: key ? { key } : {} };
}

const nodes: GraphNode[] = [
  node('dsrcA', 'datasource', '', 'dsrc:A'),
  node('dsrcB', 'datasource', '', 'dsrc:B'),
  node('dsA1', 'dataset', 'A'),
  node('dsB1', 'dataset', 'B'),
  node('domX', 'domain'),
  node('fA1', 'datasetfield'),
  node('fB1', 'datasetfield'),
  node('calA', 'metriccaliber'),
  node('calA2', 'metriccaliber'),
  node('calB', 'metriccaliber'),
  node('mShared', 'metric'),
  node('mOnlyA', 'metric'),
  node('mBare', 'metric'),
  node('dimX', 'dimension'),
  node('tA', 'table'),
  node('cA1', 'column'),
];

function edge(source: string, target: string, relation: string): GraphEdge {
  return { source, target, relation };
}

const edges: GraphEdge[] = [
  edge('domX', 'dsA1', 'HAS'),
  edge('domX', 'dsB1', 'HAS'),
  edge('dsA1', 'fA1', 'HAS'),
  edge('dsB1', 'fB1', 'HAS'),
  edge('mShared', 'calA', 'HAS'),
  edge('calA', 'dsA1', 'CALIBER_ON'),
  edge('mShared', 'calB', 'HAS'),
  edge('calB', 'dsB1', 'CALIBER_ON'),
  edge('mOnlyA', 'calA2', 'HAS'),
  edge('calA2', 'dsA1', 'CALIBER_ON'),
  edge('domX', 'mShared', 'HAS'),
  edge('domX', 'mOnlyA', 'HAS'),
  edge('domX', 'mBare', 'HAS'),
  edge('domX', 'dimX', 'HAS'),
  edge('dsrcA', 'tA', 'HAS'),
  edge('tA', 'cA1', 'HAS'),
  edge('cA1', 'fA1', 'MAPS_TO_FIELD'),
];

const dataset: GraphDataset = {
  nodes,
  edges,
  meta: {
    workspace: 'ws',
    source: 'test',
    nodeCount: nodes.length,
    edgeCount: edges.length,
    byLayer: { MG: nodes.length, TG: 0, KG: 0, UG: 0 },
  },
};

const db = createGraphDb(dataset);

// Chip A: full closure of A, none of B's exclusives.
const scopeA = new Set(db.layerScopeIds('MG', 'A'));
[
  'dsrcA',
  'dsA1',
  'domX',
  'fA1',
  'calA',
  'calA2',
  'mShared',
  'mOnlyA',
  'mBare',
  'dimX',
  'tA',
  'cA1',
].forEach((id) => assert.equal(scopeA.has(id), true, `A scope must include ${id}`));
['dsrcB', 'dsB1', 'fB1', 'calB'].forEach((id) =>
  assert.equal(scopeA.has(id), false, `A scope must exclude ${id}`),
);
assert.equal(scopeA.size, 12);

// Chip B: shared nodes visible, A's exclusives out.
const scopeB = new Set(db.layerScopeIds('MG', 'B'));
['dsrcB', 'dsB1', 'domX', 'fB1', 'calB', 'mShared', 'mBare', 'dimX'].forEach((id) =>
  assert.equal(scopeB.has(id), true, `B scope must include ${id}`),
);
['dsA1', 'fA1', 'calA', 'calA2', 'mOnlyA', 'tA', 'cA1'].forEach((id) =>
  assert.equal(scopeB.has(id), false, `B scope must exclude ${id}`),
);

// "all" keeps everything.
assert.equal(db.layerScopeIds('MG', 'all').length, nodes.length);
assert.equal(db.scopeHas('mBare', 'all'), true);

// Locate: stamped and unstamped nodes resolve to a datasource chip.
assert.equal(db.dsForNode('mOnlyA'), 'A');
assert.equal(db.dsForNode('fB1'), 'B');
assert.equal(db.dsForNode('mBare'), 'A');
assert.equal(db.datasourceList().sort().join(','), 'A,B');

// API datasource IDs must win over owner-scoped graph keys, in both shared
// and personal graphs. Equal display names must not merge different sources.
for (const owner of ['__global__', 'alice', 'bob']) {
  const anchorKeys: Record<string, string> = {
    dsrcA: `dsrc:${owner}:A`,
    dsrcB: `dsrc:${owner}:B`,
  };
  const remapId = (id: string) => anchorKeys[id] || id;
  const scopedNodes = nodes.map((n) =>
    n.type !== 'datasource'
      ? n
      : apiNodeToGraphNode({
          id: remapId(n.id),
          group: 'DataSource',
          label: '订单数据库',
          props: {
            key: remapId(n.id),
            datasource_id: n.id === 'dsrcA' ? 'A' : 'B',
            owner_id: owner,
          },
        }),
  );
  const scopedDb = createGraphDb({
    ...dataset,
    nodes: scopedNodes,
    edges: edges.map((e) => ({ ...e, source: remapId(e.source), target: remapId(e.target) })),
  });

  assert.deepEqual(
    scopedDb.datasourceList().sort(),
    ['A', 'B'],
    `${owner}: one chip per datasource ID`,
  );
  assert.deepEqual(scopedDb.datasourceNames(), { A: '订单数据库', B: '订单数据库' });
  assert.deepEqual(new Set(scopedDb.layerScopeIds('MG', 'A')), new Set([...scopeA].map(remapId)));
  assert.deepEqual(new Set(scopedDb.layerScopeIds('MG', 'B')), new Set([...scopeB].map(remapId)));
  assert.deepEqual(
    new Set(scopedDb.layerScopeIds('MG', 'all')),
    new Set(scopedNodes.map((n) => n.id)),
  );
  assert.equal(scopedDb.dsForNode(anchorKeys.dsrcA), 'A');
  assert.equal(scopedDb.dsForNode('mOnlyA'), 'A');
  assert.equal(scopedDb.dsForNode('fB1'), 'B');
  assert.equal(scopedDb.getNode(anchorKeys.dsrcA)?.props?.key, anchorKeys.dsrcA);
  assert.equal(
    scopedDb.subNeighborhood(anchorKeys.dsrcA).nodes.some((n) => n.id === 'tA'),
    true,
  );
}

console.log('graphDb.dsScope closure narrowing ok');
