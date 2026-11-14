/** Isolated MG nodes must stay expandable — otherwise both hop buttons disable. */
import assert from 'node:assert/strict';
import { createGraphDb } from './graphDb';
import type { GraphDataset, GraphNode } from '../types';

function node(id: string, label: string, type: GraphNode['type'] = 'dataset'): GraphNode {
  return { id, label, type, layer: 'MG' };
}

const isolated: GraphDataset = {
  nodes: [
    node('dom', '域', 'domain'),
    node('ds', '源', 'datasource'),
    node('a', 'A'),
    node('b', 'B'),
  ],
  edges: [],
  meta: {
    workspace: 'ws',
    source: 'test',
    nodeCount: 4,
    edgeCount: 0,
    byLayer: { MG: 4, TG: 0, KG: 0, UG: 0 },
  },
};

const db = createGraphDb(isolated);
const hop0 = db.layerCenter('MG', 'all', 0, 'dom');
assert.equal(hop0.maxHop > 0, true, 'isolated leftovers must occupy a later hop');
assert.equal(hop0.ids.size, 1);
assert.equal(hop0.ids.has('dom'), true);

const hop1 = db.layerCenter('MG', 'all', 1, 'dom');
assert.equal(hop1.ids.size, 4);
assert.equal(hop1.hop < hop1.maxHop || hop1.ids.size === hop1.total, true);

console.log('graphDb.layerCenter isolated-hop ok');
