/** Run with Node >= 22.15: node --experimental-strip-types src/services/userGraph/api.test.ts */
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

const apiUrl = new URL('./api.ts', import.meta.url).href;
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@/auth') return { url: 'ug-test:auth-format', shortCircuit: true };
    if (specifier === '@/utils/semanticConfigContract') {
      return {
        url: new URL('../../utils/semanticConfigContract.ts', import.meta.url).href,
        shortCircuit: true,
      };
    }
    if (specifier.startsWith('ug-test:')) return { url: specifier, shortCircuit: true };
    if (context.parentURL === apiUrl) {
      if (specifier === '../request') return { url: 'ug-test:request', shortCircuit: true };
      if (specifier === '../../store/authStore') return { url: 'ug-test:auth', shortCircuit: true };
      if (specifier === '../apiError') {
        return { url: new URL('../apiError.ts', apiUrl).href, shortCircuit: true };
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url === 'ug-test:auth-format')
      return {
        format: 'module',
        shortCircuit: true,
        source: `export const formatDetail = (value) => typeof value === 'string' ? value : '';`,
      };
    if (url === 'ug-test:request')
      return {
        format: 'module',
        shortCircuit: true,
        source: `
        export const calls = [];
        let response;
        export function setResponse(value) { response = value; calls.length = 0; }
        export async function request(url, options = {}) {
          calls.push({ url, method: options.method || 'GET', body: options.body });
          return response;
        }
      `,
      };
    if (url === 'ug-test:auth')
      return {
        format: 'module',
        shortCircuit: true,
        source: `
        let state;
        export function setAuth(value) { state = value; }
        export const useAuthStore = { getState: () => state };
      `,
      };
    return nextLoad(url, context);
  },
});

// @ts-expect-error test-only virtual module
const { calls, setResponse } = (await import('ug-test:request')) as {
  calls: Array<{ url: string; method: string; body?: string }>;
  setResponse: (response: unknown) => void;
};
// @ts-expect-error test-only virtual module
const { setAuth } = (await import('ug-test:auth')) as {
  setAuth: (state: { authEnabled: boolean; userId: string; capabilities: string[] }) => void;
};
const { ApiRequestError } = await import('../apiError.ts');
const { userGraphApi } = await import('./api.ts');

const mutations = [
  {
    method: 'PATCH',
    path: '/me',
    action: undefined,
    call: (subject: string) => userGraphApi.patchUser(subject, { display_name: 'Alice' }),
  },
  {
    method: 'POST',
    path: '/me/memories',
    action: 'created',
    call: (subject: string) =>
      userGraphApi.createMemory(subject, {
        type: 'preference',
        description: 'Keep answers concise.',
      }),
  },
  {
    method: 'PATCH',
    path: '/me/memories/memory-1',
    action: 'updated',
    call: (subject: string) =>
      userGraphApi.patchMemory(subject, 'memory-1', {
        description: 'Keep answers concise.',
      }),
  },
  {
    method: 'DELETE',
    path: '/me/memories/memory-1',
    action: 'revoked',
    call: (subject: string) => userGraphApi.revokeMemory(subject, 'memory-1'),
  },
];

try {
  // Every mutation, including the explicit 'me' alias, uses the self endpoint.
  setAuth({ authEnabled: true, userId: 'alice', capabilities: ['tg_kg:self'] });
  for (const subject of ['alice', 'me']) {
    for (const mutation of mutations) {
      setResponse({ ok: true, data: { action: mutation.action, status: 'revoked' } });
      await mutation.call(subject);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, `/api/v1/ug${mutation.path}`);
      assert.equal(calls[0].method, mutation.method);
    }
  }

  // Reviewing other users does not grant a writable explicit-user endpoint.
  setAuth({
    authEnabled: true,
    userId: 'alice',
    capabilities: ['tg_kg:self', 'tg_kg:others_read'],
  });
  for (const mutation of mutations) {
    setResponse(undefined);
    await assert.rejects(
      mutation.call('bob'),
      (error) => error instanceof ApiRequestError && error.status === 403,
    );
    assert.equal(calls.length, 0);
  }

  // Local mode and missing identities cannot fall back to removed write routes.
  for (const auth of [
    { authEnabled: false, userId: '' },
    { authEnabled: false, userId: 'alice' },
    { authEnabled: true, userId: '' },
  ]) {
    setAuth({ ...auth, capabilities: ['tg_kg:self'] });
    for (const subject of ['alice', 'me']) {
      for (const mutation of mutations) {
        setResponse(undefined);
        await assert.rejects(
          mutation.call(subject),
          (error) => error instanceof ApiRequestError && error.status === 401,
        );
        assert.equal(calls.length, 0);
      }
    }
  }

  setAuth({
    authEnabled: true,
    userId: 'alice',
    capabilities: ['tg_kg:self', 'tg_kg:others_read'],
  });
  setResponse({ ok: true, data: { user_id: 'alice' } });
  await userGraphApi.getUser('alice');
  assert.equal(calls[0].url, '/api/v1/ug/me');
  setResponse({ ok: true, data: { user_id: 'bob' } });
  await userGraphApi.getUser('bob');
  assert.equal(calls[0].url, '/api/v1/ug/users/bob');
  setResponse({ ok: true, data: [], meta: { page: 1, page_size: 20, total: 0, has_more: false } });
  await userGraphApi.listMemories('bob');
  assert.equal(calls[0].url, '/api/v1/ug/users/bob/memories?page=1&page_size=20');
  setResponse({ ok: true, data: { id: 'memory-1' } });
  await userGraphApi.getMemory('bob', 'memory-1');
  assert.equal(calls[0].url, '/api/v1/ug/users/bob/memories/memory-1');
} finally {
  hooks.deregister();
}
console.log(
  'UG API: self writes, removed-route fallbacks, identity checks and admin reads passed.',
);
