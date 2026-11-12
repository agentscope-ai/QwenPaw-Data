import assert from 'node:assert/strict';
import test from 'node:test';
import { answerToolConfirmation } from '../../api/toolConfirmation.js';
import { ApiRequestError } from '../../../../services/request.js';

async function withFetch(mock: typeof fetch, run: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    await run();
  } finally {
    globalThis.fetch = original;
  }
}

test('confirmation uses the real Host transport, encoded scope, cookie credentials and exact decision body', async () => {
  await withFetch(
    async (input, init) => {
      assert.equal(input, '/api/sessions/s%2F1/chats/c%201/tool-confirmation/answer');
      assert.equal(init?.method, 'POST');
      assert.equal(init?.credentials, 'include');
      assert.equal(new Headers(init?.headers).get('Content-Type'), 'application/json');
      assert.deepEqual(JSON.parse(String(init?.body)), { call_id: 'call', decision: 'denied' });
      return Response.json({ chat: { id: 'c 1', session_id: 's/1', status: 'running' } });
    },
    async () => {
      assert.equal(
        (await answerToolConfirmation('s/1', 'c 1', { call_id: 'call', decision: 'denied' })).chat
          .status,
        'running',
      );
    },
  );
});

test('malformed success, missing chat and wrong ownership are unknown rather than accepted', async () => {
  for (const payload of [
    null,
    {},
    { chat: { id: 'other', session_id: 's' } },
    { chat: { id: 'c', session_id: 'other' } },
  ]) {
    await withFetch(
      async () => Response.json(payload),
      async () => {
        await assert.rejects(
          answerToolConfirmation('s', 'c', { call_id: 'call', decision: 'approved' }),
          /Invalid tool confirmation response/,
        );
      },
    );
  }
  await withFetch(
    async () => new Response('<html>ok</html>'),
    async () => {
      await assert.rejects(
        answerToolConfirmation('s', 'c', { call_id: 'call', decision: 'approved' }),
      );
    },
  );
});

test('structured error semantics survive transport, without automatic retry on 5xx', async () => {
  let calls = 0;
  await withFetch(
    async () => {
      calls++;
      return Response.json({ error: { code: 'CONFLICT', message: 'chat ended' } }, { status: 409 });
    },
    async () => {
      await assert.rejects(
        answerToolConfirmation('s', 'c', { call_id: 'call', decision: 'approved' }),
        (error) => {
          assert.ok(error instanceof ApiRequestError);
          assert.equal(error.status, 409);
          assert.equal(error.code, 'CONFLICT');
          return true;
        },
      );
    },
  );
  await withFetch(
    async () => {
      calls++;
      return new Response('unavailable', { status: 503 });
    },
    async () => {
      await assert.rejects(
        answerToolConfirmation('s', 'c', { call_id: 'call', decision: 'approved' }),
      );
    },
  );
  assert.equal(calls, 2);
});

test('confirmation retains the existing single 401 refresh retry behavior', async () => {
  const calls: string[] = [];
  await withFetch(
    async (input) => {
      calls.push(String(input));
      return calls.length === 1
        ? new Response(null, { status: 401 })
        : Response.json({ chat: { id: 'c', session_id: 's' } });
    },
    async () => {
      await answerToolConfirmation('s', 'c', { call_id: 'call', decision: 'approved' });
    },
  );
  assert.deepEqual(calls, [
    '/api/sessions/s/chats/c/tool-confirmation/answer',
    '/api/auth/refresh',
    '/api/sessions/s/chats/c/tool-confirmation/answer',
  ]);
});
