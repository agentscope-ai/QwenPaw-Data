import assert from 'node:assert/strict';
import test from 'node:test';
import { fetchAuthStatus } from './auth.ts';

test('portal bootstrap uses the gateway and fails closed on an invalid status', async (context) => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { QWENPAW_DATA_CONSOLE: { databridge: {
      base_path: '/databridge', api_base_url: '/databridge', auth_mode: 'portal', console_url: '/console',
    } } },
  });
  context.after(() => {
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  });
  let body: unknown = { required: false };
  context.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, init?: RequestInit) => {
    assert.equal(url, '/databridge/api/auth/status');
    assert.equal(new Headers(init?.headers).get('X-QwenPaw-Console'), '1');
    assert.equal(init?.credentials, 'include');
    return Response.json(body);
  });
  assert.equal((await fetchAuthStatus()).enabled, false);
  body = {};
  await assert.rejects(fetchAuthStatus(), /Portal authentication status is invalid/);
  body = { required: true };
  await assert.rejects(fetchAuthStatus(), /Portal authentication status is invalid/);
});
