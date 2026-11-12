import assert from 'node:assert/strict';
import test from 'node:test';
import type { TraceMessage } from '../../api/types.js';
import { isToolConfirmationResult, toolConfirmationCallId } from '../chat/toolConfirmation.js';
import { ApiRequestError } from '../../../../services/request.js';
import {
  confirmationIdentity,
  confirmationKey,
  createToolConfirmationSubmissions,
} from '../chat/toolConfirmationSubmission.js';

const target = { sessionId: 'session', chatId: 'chat', callId: 'call' };
const access = { assertEligible: () => {}, isCurrent: () => true };

test('empty waiting output does not clear an accepted claim or reopen it on refresh', async () => {
  const disk = storage();
  const store = createToolConfirmationSubmissions('user', () => disk);
  await store.submit(target, 'approved', access, async () => {});
  const placeholder: TraceMessage = {
    id: 'out',
    object: 'message',
    chat_id: 'chat',
    sequence: 4,
    type: 'plugin_call_output',
    role: 'tool',
    status: 'in_progress',
    source_id: 'call',
    metadata: null,
    content: [],
  };
  const resolvedCallIds = new Set(
    [placeholder]
      .filter(isToolConfirmationResult)
      .map(toolConfirmationCallId)
      .filter((id): id is string => id !== null),
  );
  store.reconcile('session', [{ chatId: 'chat', terminal: false, resolvedCallIds }]);
  const restored = createToolConfirmationSubmissions('user', () => disk);
  assert.equal(restored.getSnapshot().records[confirmationKey(target)].status, 'accepted');
  await restored.submit(target, 'denied', access, async () => assert.fail('must not resend'));
});
function storage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
}
function deferred() {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

test('rapid approve/deny claims synchronously and records submission before sending', async () => {
  const disk = storage();
  const store = createToolConfirmationSubmissions('user', () => disk);
  const response = deferred();
  let calls = 0;
  const send = () => {
    calls++;
    assert.match([...disk.values.values()][0], /submitting/);
    return response.promise;
  };
  const first = store.submit(target, 'approved', access, send);
  await store.submit(target, 'denied', access, send);
  assert.equal(calls, 1);
  response.resolve();
  await first;
  assert.equal(store.getSnapshot().records[confirmationKey(target)].status, 'accepted');
  await store.submit(target, 'denied', access, send);
  assert.equal(calls, 1);
});

test('distinct calls and chats are independent, and session/identity keys never collide', async () => {
  const disk = storage();
  const store = createToolConfirmationSubmissions('user', () => disk);
  let calls = 0;
  for (const item of [
    target,
    { ...target, callId: 'next' },
    { ...target, chatId: 'next' },
    { ...target, sessionId: 'next' },
  ]) {
    await store.submit(item, 'approved', access, async () => {
      calls++;
    });
  }
  assert.equal(calls, 4);
  assert.deepEqual(
    createToolConfirmationSubmissions('another-user', () => disk).getSnapshot().records,
    {},
  );
  const user = { user_id: 'u', tenant_id: 't', active_workspace_id: 'w' };
  assert.notEqual(
    confirmationIdentity('host', user),
    confirmationIdentity('host', { ...user, active_workspace_id: 'other' }),
  );
  assert.equal(confirmationIdentity('host', null), null);
});

test('reload restores submitting as unknown and accepted as locked without storing arguments', async () => {
  const disk = storage();
  const store = createToolConfirmationSubmissions('user', () => disk);
  const response = deferred();
  const first = store.submit(target, 'approved', access, () => response.promise);
  const restored = createToolConfirmationSubmissions('user', () => disk);
  assert.equal(restored.getSnapshot().records[confirmationKey(target)].status, 'unknown');
  let resent = false;
  await restored.submit(target, 'denied', access, async () => {
    resent = true;
  });
  assert.equal(resent, false);
  response.resolve();
  await first;
  assert.equal(
    createToolConfirmationSubmissions('user', () => disk).getSnapshot().records[
      confirmationKey(target)
    ].status,
    'accepted',
  );
  const saved = JSON.parse([...disk.values.values()][0]);
  assert.deepEqual(Object.keys(saved.records[0]).sort(), [
    'callId',
    'chatId',
    'cleanupAt',
    'decision',
    'sessionId',
    'status',
  ]);
});

test('only explicit validation rejection can retry; access/terminal failures remain blocked', async () => {
  for (const status of [400, 422, 401, 403, 404, 409, 500, 502]) {
    const store = createToolConfirmationSubmissions('user', storage);
    let calls = 0;
    await store.submit(target, 'denied', access, async () => {
      calls++;
      throw new ApiRequestError({ status, message: 'rejected' });
    });
    assert.equal(
      store.getSnapshot().records[confirmationKey(target)].status,
      [400, 422].includes(status) ? 'retry' : status < 500 ? 'blocked' : 'unknown',
    );
    await store.submit(target, 'approved', access, async () => {
      calls++;
    });
    assert.equal(calls, [400, 422].includes(status) ? 2 : 1);
  }
});

test('network errors and invalid responses stay unknown and do not retry', async () => {
  for (const error of [new TypeError('Failed to fetch'), new Error('Invalid response')]) {
    const store = createToolConfirmationSubmissions('user', storage);
    await store.submit(target, 'approved', access, async () => {
      throw error;
    });
    assert.equal(store.getSnapshot().records[confirmationKey(target)].status, 'unknown');
    await store.submit(target, 'approved', access, async () => assert.fail('must not resend'));
  }
});

test('output before response and terminal cleanup cannot be reversed by a late response', async () => {
  for (const terminal of [false, true]) {
    const disk = storage();
    const store = createToolConfirmationSubmissions('user', () => disk);
    const response = deferred();
    const pending = store.submit(target, 'approved', access, () => response.promise);
    store.reconcile('other-session', [
      { chatId: 'chat', terminal: true, resolvedCallIds: new Set() },
    ]);
    assert.ok(store.getSnapshot().records[confirmationKey(target)]);
    store.reconcile('session', [
      { chatId: 'chat', terminal, resolvedCallIds: new Set(terminal ? [] : ['call']) },
    ]);
    response.resolve();
    await pending;
    assert.deepEqual(store.getSnapshot().records, {});
    assert.equal(disk.values.size, 0);
  }
});

test('navigation or identity changes isolate late success and error callbacks', async () => {
  for (const fail of [false, true]) {
    const disk = storage();
    const old = createToolConfirmationSubmissions('old-user', () => disk);
    const next = createToolConfirmationSubmissions('new-user', () => disk);
    const response = deferred();
    let current = true;
    const pending = old.submit(
      target,
      'approved',
      { ...access, isCurrent: () => current },
      () => response.promise,
    );
    current = false;
    if (fail) response.reject(new ApiRequestError({ status: 400, message: 'stale' }));
    else response.resolve();
    await pending;
    assert.equal(old.getSnapshot().records[confirmationKey(target)].status, 'unknown');
    assert.deepEqual(next.getSnapshot().records, {});
  }
});

test('eligibility rejection never sends or claims, including after a recoverable validation error', async () => {
  const store = createToolConfirmationSubmissions('user', storage);
  const ineligible = {
    ...access,
    assertEligible: () => {
      throw new Error('expired or read-only');
    },
  };
  await assert.rejects(
    store.submit(target, 'approved', ineligible, async () => assert.fail('sent')),
  );
  assert.deepEqual(store.getSnapshot().records, {});
  await store.submit(target, 'approved', access, async () => {
    throw new ApiRequestError({ status: 422, message: 'invalid' });
  });
  await assert.rejects(
    store.submit(target, 'approved', ineligible, async () => assert.fail('sent')),
  );
});

test('malformed or expired recovery data and unavailable storage are explicit, not silent', async () => {
  const disk = storage();
  const store = createToolConfirmationSubmissions(
    'user',
    () => disk,
    () => 0,
  );
  await store.submit(target, 'approved', access, async () => {});
  const key = [...disk.values.keys()][0];
  assert.deepEqual(
    createToolConfirmationSubmissions(
      'user',
      () => disk,
      () => 8 * 86400000,
    ).getSnapshot().records,
    {},
  );
  disk.setItem(key, '{broken');
  assert.equal(
    createToolConfirmationSubmissions('user', () => disk).getSnapshot().recoveryLimited,
    true,
  );
  const failed = createToolConfirmationSubmissions('user', () => {
    throw new Error('denied');
  });
  await failed.submit(target, 'approved', access, async () => {});
  assert.equal(failed.getSnapshot().recoveryLimited, true);
  assert.equal(failed.getSnapshot().records[confirmationKey(target)].status, 'accepted');
  const quota = createToolConfirmationSubmissions('user', () => ({
    ...storage(),
    setItem: () => {
      throw new Error('quota');
    },
  }));
  await quota.submit(target, 'approved', access, async () => {});
  assert.equal(quota.getSnapshot().recoveryLimited, true);
  await quota.submit(target, 'denied', access, async () => assert.fail('must retain memory claim'));
});
