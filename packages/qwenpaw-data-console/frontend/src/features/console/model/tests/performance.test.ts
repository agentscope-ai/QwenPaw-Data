import assert from 'node:assert/strict';
import test from 'node:test';
import { createReadCache, preserveUnchangedList } from '../chat/readCache.js';
import { createDeferredBatch } from '../chat/deferredBatch.js';
import { createModelConfigurationAdmission } from '../chat/modelConfigurationGate.js';

test('reads coalesce, expire, and failed reads can retry', async () => {
  let now = 0,
    calls = 0;
  const cache = createReadCache<number>(100, () => now);
  const load = async () => ++calls;
  assert.deepEqual(await Promise.all([cache.get('a', load), cache.get('a', load)]), [1, 1]);
  assert.equal(await cache.get('a', load), 1);
  now = 100;
  assert.equal(await cache.get('a', load), 2);
  await assert.rejects(
    cache.get('bad', async () => {
      throw Error('network');
    }),
  );
  assert.equal(await cache.get('bad', load), 3);
  assert.equal(await createReadCache<number>(100).get('a', load), 4);
});

test('reaction mutation wins over an older pending read', async () => {
  const cache = createReadCache<string | null>(1000);
  let resolve!: (value: string | null) => void;
  const pending = cache.get(
    'reaction',
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  await Promise.resolve();
  cache.set('reaction', 'like');
  resolve(null);
  await pending;
  assert.equal(await cache.get('reaction', async () => null), 'like');
});

test('configuration admission is scoped, one-shot and short lived', () => {
  let now = 0;
  const receipt = createModelConfigurationAdmission('user:session', () => now);
  assert.equal(receipt.consume('user:session'), true);
  assert.equal(receipt.consume('user:session'), false);
  const switched = createModelConfigurationAdmission('user:session', () => now);
  assert.equal(switched.consume('other:session'), false);
  const expired = createModelConfigurationAdmission('user:session', () => now);
  now = 10_000;
  assert.equal(expired.consume('user:session'), false);
});

test('nearby trace results batch but canceled navigation never publishes', () => {
  let flush!: () => void,
    schedules = 0;
  const batches: number[][] = [];
  const batch = createDeferredBatch<number>(
    (items) => batches.push(items),
    (fn) => {
      schedules++;
      flush = fn;
      return () => {};
    },
  );
  batch.push(1);
  batch.push(2);
  assert.equal(schedules, 1);
  flush();
  assert.deepEqual(batches, [[1, 2]]);
  batch.push(3);
  batch.dispose();
  flush();
  batch.push(4);
  assert.deepEqual(batches, [[1, 2]]);
});

test('unchanged catalogs preserve references while title, access-list and ordering changes propagate', () => {
  const previous = [
    { id: 'one', title: 'A' },
    { id: 'two', title: 'B' },
  ];
  assert.equal(preserveUnchangedList(previous, structuredClone(previous)), previous);
  for (const next of [
    previous.slice(1),
    [...previous].reverse(),
    [{ id: 'one', title: 'renamed' }, previous[1]],
  ]) {
    assert.equal(preserveUnchangedList(previous, next), next);
  }
});
