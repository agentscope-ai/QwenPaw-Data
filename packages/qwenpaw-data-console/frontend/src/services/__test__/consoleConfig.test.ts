import test from 'node:test';
import assert from 'node:assert/strict';
import { getApiUrl } from '../config.js';
import { fetchWithAuthRetry } from '../auth.js';

test('GEO paths use the official v1 prefix exactly once', () => {
  assert.equal(getApiUrl('/sessions'), '/api/v1/sessions');
  assert.equal(getApiUrl('/api/v1/console/chat'), '/api/v1/console/chat');
  assert.equal(getApiUrl('/sessions/s1/chats/c1/events?after_sequence_number=5'), '/api/v1/sessions/s1/chats/c1/events?after_sequence_number=5');
});

test('SSE requests carry gateway header without a browser API key', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (_url, init) => {
      const headers = new Headers(init?.headers);
      assert.equal(headers.get('X-QwenPaw-Console'), '1');
      assert.equal(headers.get('Accept'), 'text/event-stream');
      assert.equal(headers.has('Authorization'), false);
      return new Response('ok');
    };
    assert.equal((await fetchWithAuthRetry('/api/v1/sessions', { headers: { Accept: 'text/event-stream' } })).status, 200);
  } finally { globalThis.fetch = original; }
});

test('gateway marker is limited to same-origin requests and preserves Request headers', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (input, init) => {
      const headers = new Headers(init?.headers);
      assert.equal(headers.get('X-QwenPaw-Console'), String(input).includes('remote.invalid') ? null : '1');
      if (input instanceof Request) assert.equal(headers.get('Accept'), 'text/event-stream');
      return new Response('ok');
    };
    await fetchWithAuthRetry(new Request('http://localhost/api/v1/sessions', {
      headers: { Accept: 'text/event-stream' },
    }));
    await fetchWithAuthRetry('https://remote.invalid/api/v1/sessions');
  } finally { globalThis.fetch = original; }
});

import { getRuntimeConfig } from '../runtimeConfig.js';

test('the same frontend accepts domain config without a rebuild', () => {
  assert.equal(getRuntimeConfig().title, 'QwenPaw Data');
  Object.assign(globalThis, { window: { QWENPAW_DATA_CONSOLE: {
    title: 'Econ', welcome_title: '经济分析', sample_questions: ['消费趋势如何？'],
    capabilities: { contextConsole: true },
  } } });
  try {
    const value = getRuntimeConfig();
    assert.equal(value.title, 'Econ');
    assert.equal(value.welcome_title, '经济分析');
    assert.deepEqual(value.sample_questions, ['消费趋势如何？']);
    assert.equal(value.capabilities.contextConsole, true);
    assert.equal(value.capabilities.skillsHub, true);
    assert.equal(value.capabilities.channels, true);
    assert.equal(value.capabilities.mcpHub, false);
  } finally {
    Reflect.deleteProperty(globalThis, 'window');
  }
});
