import assert from 'node:assert/strict';
import test from 'node:test';
import { createSettlementCenterStore } from '../settlement/settlementCenterStore.js';

test('unchanged background refresh preserves the snapshot without notifying subscribers', async () => {
  const store = createSettlementCenterStore('workspace', {
    cards: async () => [],
    proposals: async () => [],
  });
  await store.refresh();
  const before = store.getSnapshot();
  let notifications = 0;
  store.subscribe(() => {
    notifications++;
  });
  await store.refresh(true);
  assert.equal(store.getSnapshot(), before);
  assert.equal(notifications, 0);
});

test('background refresh reports failures and clears them after recovery', async () => {
  let fail = false;
  const store = createSettlementCenterStore('workspace', {
    cards: async () => {
      if (fail) throw new Error('offline');
      return [];
    },
    proposals: async () => [],
  });
  await store.refresh();
  fail = true;
  await store.refresh(true);
  assert.equal(store.getSnapshot().cards.error?.message, 'offline');
  assert.equal(store.getSnapshot().cards.loaded, true);
  fail = false;
  await store.refresh(true);
  assert.equal(store.getSnapshot().cards.error, null);
});
