import assert from 'node:assert/strict';
import test from 'node:test';
import { loadAllConsoleSessions } from '../chat/loadAllConsoleSessions.js';

test('a superseded catalog stops before requesting its remaining pages', async () => {
  const controller = new AbortController();
  let requests = 0;
  await assert.rejects(
    loadAllConsoleSessions(async () => {
      requests++;
      controller.abort();
      return { items: [], total: 400 };
    }, controller.signal),
    { name: 'AbortError' },
  );
  assert.equal(requests, 1);
});
