import assert from 'node:assert/strict';
import type { SemanticConfigPage } from '../types/semanticConfig.ts';
import { readUrlSearchValues } from './urlSearch.ts';
import {
  loadSemanticDeepLinkInOrder,
  loadSemanticRowsByParent,
  locateSemanticRecord,
  parseSemanticDatasourceId,
  parseSemanticObjectId,
} from './semanticDeepLink.ts';

assert.equal(parseSemanticDatasourceId(' mysql-main '), 'mysql-main');
assert.equal(parseSemanticDatasourceId('  '), undefined);
assert.equal(parseSemanticObjectId('42'), 42);
for (const invalidId of ['', '0', '-1', '1.5', '1e3', 'NaN', '9007199254740992']) {
  assert.equal(parseSemanticObjectId(invalidId), undefined, `accepted invalid ID: ${invalidId}`);
}
assert.deepEqual(
  readUrlSearchValues<{ datasource_id: string; domain_id: number }>(
    new URLSearchParams('datasourceId=legacy&domainId=9'),
    {
      datasource_id: { parse: parseSemanticDatasourceId },
      domain_id: { parse: parseSemanticObjectId },
    },
  ),
  {},
);

const loadOrder: string[] = [];
await loadSemanticDeepLinkInOrder(
  [
    async () => {
      loadOrder.push('datasource');
    },
    async () => {
      loadOrder.push('domain');
    },
  ],
  async () => {
    loadOrder.push('rows');
  },
);
assert.deepEqual(loadOrder, ['datasource', 'domain', 'rows']);

interface Item {
  id: number;
  name: string;
}
const pages: Record<number, SemanticConfigPage<Item>> = {
  1: { records: [{ id: 1, name: 'first' }], total: 3, page: 1, size: 1 },
  2: { records: [{ id: 2, name: 'second' }], total: 3, page: 2, size: 1 },
  3: { records: [{ id: 3, name: 'third' }], total: 3, page: 3, size: 1 },
};
const visitedPages: number[] = [];
const laterPageMatch = await locateSemanticRecord({
  targetId: 3,
  size: 1,
  getRecordId: (item: Item) => item.id,
  loadPage: async (page) => {
    visitedPages.push(page);
    return pages[page];
  },
});
assert.equal(laterPageMatch?.record.name, 'third');
assert.equal(laterPageMatch?.page, 3);
assert.deepEqual(visitedPages, [1, 2, 3]);

const missingMatch = await locateSemanticRecord({
  targetId: 99,
  size: 1,
  getRecordId: (item: Item) => item.id,
  loadPage: async (page) => pages[page],
});
assert.equal(missingMatch, undefined);
await assert.rejects(
  () =>
    locateSemanticRecord({
      targetId: 1,
      getRecordId: (item: Item) => item.id,
      loadPage: async () => pages[2],
    }),
  /does not match the requested page/,
);

const dimensionRows = await loadSemanticRowsByParent({
  targetId: 3,
  size: 1,
  getRecordId: (item: Item) => item.id,
  loadPage: async (page) => pages[page],
  loadRows: async () => ({
    records: [{ id: 10 }, { id: 11 }],
    total: 2,
    page: 1,
    size: 20,
  }),
});
assert.deepEqual(dimensionRows?.rows.records, [{ id: 10 }, { id: 11 }]);

const metricRows = await loadSemanticRowsByParent({
  targetId: 2,
  size: 1,
  getRecordId: (item: Item) => item.id,
  loadPage: async (page) => pages[page],
  loadRows: async () => ({
    records: [{ id: 20 }, { id: 21 }, { id: 22 }],
    total: 3,
    page: 1,
    size: 20,
  }),
});
assert.equal(metricRows?.rows.records.length, 3);
