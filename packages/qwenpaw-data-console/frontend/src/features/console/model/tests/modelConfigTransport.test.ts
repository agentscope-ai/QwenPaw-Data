import assert from 'node:assert/strict';
import test from 'node:test';
import { modelConfigApi } from '../../api/modelConfig.js';
import {
  ApiRequestError,
  request,
  requestAtUrl,
  requestFile,
} from '../../../../services/request.js';

async function withFetch(mock: typeof fetch, run: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    await run();
  } finally {
    globalThis.fetch = original;
  }
}

test('Context uses its root route, cookie identity and abort signal', async () => {
  const controller = new AbortController();
  await withFetch(
    async (input, init) => {
      assert.equal(input, '/api/system/model-config/embedding/test');
      assert.equal(init?.credentials, 'include');
      assert.equal(init?.signal, controller.signal);
      assert.equal(new Headers(init?.headers).get('Content-Type'), 'application/json');
      assert.deepEqual(JSON.parse(String(init?.body)), { model: 'embedding-test', dim: 64 });
      return Response.json({ success: true, message: 'ok', detected_dim: 64 });
    },
    async () => {
      const result = await modelConfigApi.testEmbedding(
        { model: 'embedding-test', dim: 64 },
        controller.signal,
      );
      assert.equal(result.detected_dim, 64);
    },
  );
});

test('Context refresh retries the same root route once', async () => {
  const calls: string[] = [];
  await withFetch(
    async (input) => {
      calls.push(String(input));
      return calls.length === 1
        ? new Response(null, { status: 401 })
        : Response.json({ configured: false });
    },
    async () => {
      await modelConfigApi.get();
    },
  );
  assert.deepEqual(calls, [
    '/api/system/model-config/',
    '/api/auth/refresh',
    '/api/system/model-config/',
  ]);
});

test('Context workspace conflicts preserve status, code and details', async () => {
  const details = { workspace: { dim: 128 }, mine: { dim: 64 } };
  await withFetch(
    async () =>
      Response.json(
        {
          detail: {
            error: {
              code: 'embedding_space_conflict',
              message: 'Workspace conflict',
              details,
            },
          },
        },
        { status: 409 },
      ),
    async () => {
      await assert.rejects(modelConfigApi.updateEmbedding({ dim: 64 }), (error: unknown) => {
        assert.ok(error instanceof ApiRequestError);
        assert.equal(error.status, 409);
        assert.equal(error.code, 'embedding_space_conflict');
        assert.deepEqual(error.details, details);
        return true;
      });
    },
  );
});

test('Host JSON and file requests retain their existing API path and response behavior', async () => {
  const calls: string[] = [];
  await withFetch(
    async (input) => {
      calls.push(String(input));
      return calls.length === 1 ? Response.json({ id: 'session' }) : new Response('file');
    },
    async () => {
      assert.deepEqual(await request('/sessions'), { id: 'session' });
      assert.equal(await (await requestFile('/attachments/file')).text(), 'file');
    },
  );
  assert.deepEqual(calls, ['/api/sessions', '/api/attachments/file']);
});

test('Resolved URL requests support 204 and encode job IDs', async () => {
  await withFetch(
    async (input) => {
      assert.equal(input, '/api/system/model-config/embedding/jobs/id%2Fwith%20space/retry');
      return new Response(null, { status: 204 });
    },
    async () => {
      assert.equal(await modelConfigApi.retryRebuildJob('id/with space'), undefined);
    },
  );
  await withFetch(
    async () => new Response('plain text'),
    async () => {
      assert.equal(await requestAtUrl('/api/system/model-config/'), 'plain text');
    },
  );
});
