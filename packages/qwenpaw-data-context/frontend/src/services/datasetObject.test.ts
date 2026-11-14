import assert from 'node:assert/strict';
import test from 'node:test';
import { getDatasetMeta } from './datasetManagement.ts';

// @ts-expect-error Existing test-only transport provided by the project loader.
const harness = await import('semantic-test:request');

test('exact dataset lookup normalizes nullable draft fields and preserves source identity', async () => {
  harness.resetRequestTestHarness();
  harness.setRequestTestResponse({
    dataset_id: 7,
    domain_id: 2,
    datasource_id: 'source-a',
    description: null,
  });
  const result = await getDatasetMeta(7);
  assert.equal(result.datasource_id, 'source-a');
  assert.equal(result.record.description, '');
  assert.equal(result.record.dataset_id, 7);
  assert.equal(harness.requestTestCalls.length, 1);
  assert.equal(harness.requestTestCalls[0].method, 'get');
  assert.match(harness.requestTestCalls[0].url, /\/dataset-meta\/7$/);
});

test('wrong IDs, missing domain/source and nontext descriptions never become valid objects', async () => {
  for (const patch of [
    { dataset_id: 8 },
    { domain_id: null },
    { datasource_id: null },
    { description: {} },
  ]) {
    harness.setRequestTestResponse({
      dataset_id: 7,
      domain_id: 2,
      datasource_id: 'source-a',
      ...patch,
    });
    await assert.rejects(getDatasetMeta(7));
  }
  await assert.rejects(getDatasetMeta(0), /INVALID_ARGUMENT/);
});
