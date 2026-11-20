import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat } from '../../api/types.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { selectResponseFailure } from '../runtime/responseFailure.js';
import { parseRuntimeStreamChunk } from '../runtime/runtimeMessages.js';

const codes = [
  'MODEL_AUTH',
  'MODEL_QUOTA',
  'MODEL_RATE_LIMIT',
  'MODEL_NOT_FOUND',
  'CONTEXT_OVERFLOW',
  'REACT_MAX_ITERS',
  'MODEL_UNAVAILABLE',
  'CONFIG',
  'INTERNAL',
  'VALIDATION',
];
for (const code of codes) {
  test(`failed response and restored history expose the backend message: ${code}`, () => {
    const error = {
      code,
      message: '后端面向用户的说明',
      details: { cause: 'private upstream exception' },
    };
    const event = {
      object: 'response' as const,
      id: 'response-1',
      session_id: 'session-1',
      chat_id: 'chat-1',
      sequence_number: 1,
      status: 'failed' as const,
      usage: null,
      error,
    };
    const projection = projectStreamEvent(createChatStreamProjection(chat()), event);
    const chunk = parseRuntimeStreamChunk(JSON.stringify(event));
    assert.equal(chunk.object, 'response');
    if (chunk.object !== 'response') throw new Error('Expected response');
    const expected = {
      code,
      message: error.message,
      canConfigure: ['MODEL_AUTH', 'MODEL_NOT_FOUND', 'CONFIG', 'REACT_MAX_ITERS'].includes(code),
    };
    assert.deepEqual(selectResponseFailure(projection.chat, chunk), expected);
    assert.deepEqual(selectResponseFailure(undefined, chunk), expected);
    assert.deepEqual(selectResponseFailure(chat({ status: 'failed', error }), {}), expected);
    assert.equal(JSON.stringify(expected).includes(error.details.cause), false);
  });
}

test('unknown codes display backend text; missing errors use the UI fallback', () => {
  assert.deepEqual(
    selectResponseFailure(undefined, {
      status: 'failed',
      error: { code: 'NEW_CODE', message: 'New failure' },
    }),
    { code: 'NEW_CODE', message: 'New failure', canConfigure: false },
  );
  assert.deepEqual(selectResponseFailure(chat({ status: 'failed' }), {}), {
    code: '',
    message: '',
    canConfigure: false,
  });
  for (const status of ['running', 'completed', 'canceled']) {
    assert.equal(
      selectResponseFailure(
        chat({
          status: status as ConsoleChat['status'],
          error: { code: 'MODEL_AUTH', message: 'stale' },
        }),
        {},
      ),
      null,
    );
  }
});

function chat(overrides: Partial<ConsoleChat> = {}): ConsoleChat {
  return {
    id: 'chat-1',
    session_id: 'session-1',
    sequence: 1,
    user_input: '分析 DAU',
    datasource_id: 'datasource-1',
    kind: 'planned',
    forkable: false,
    status: 'running',
    last_sequence_number: 0,
    started_at: '2026-07-30T10:00:00+08:00',
    completed_at: null,
    active_duration_ms: 0,
    error: null,
    plan: null,
    segments: [],
    biz_events: [],
    artifacts: [],
    followup: null,
    ...overrides,
  };
}
