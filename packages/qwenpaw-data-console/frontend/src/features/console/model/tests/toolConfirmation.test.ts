import assert from 'node:assert/strict';
import test from 'node:test';
import type { TraceMessage } from '../../api/types.js';
import {
  selectToolConfirmations,
  toolConfirmationAvailability,
  visibleToolConfirmations,
} from '../chat/toolConfirmation.js';
import { selectPendingAskUserQuestion } from '../chat/askUserQuestion.js';

function call(
  overrides: Partial<TraceMessage> = {},
  data: Record<string, unknown> = {},
): TraceMessage {
  return {
    id: 'message-id',
    object: 'message',
    chat_id: 'chat',
    sequence: 1,
    type: 'plugin_call',
    role: 'assistant',
    status: 'completed',
    source_id: 'source',
    metadata: { awaiting_confirmation: true, expires_at: '2026-10-29T12:30:00Z' },
    content: [
      {
        object: 'content',
        type: 'data',
        delta: false,
        index: 0,
        status: 'completed',
        msg_id: 'message-id',
        text: null,
        image_url: null,
        format: null,
        video_url: null,
        filename: null,
        file_url: null,
        refusal: null,
        data: { name: 'Bash', call_id: 'call', arguments: '{"command":"pwd"}', ...data },
      },
    ],
    ...overrides,
  };
}

test('only complete server-marked calls with usable identity/arguments are candidates', () => {
  const invalid = [
    call({ status: 'in_progress' }),
    call({ metadata: null }),
    call({ metadata: { awaiting_confirmation: 'true' } }),
    call({ chat_id: 'other' }),
    call({}, { name: 'ask_user_question' }),
    call({}, { name: '' }),
    call({ source_id: '' }, { call_id: ' ' }),
    call({}, { arguments: undefined }),
  ];
  assert.deepEqual(selectToolConfirmations(invalid, 'chat'), []);
  const [candidate] = selectToolConfirmations([call()], 'chat');
  assert.equal(candidate.callId, 'call');
  assert.equal(candidate.argumentsText, '{\n  "command": "pwd"\n}');
  assert.equal(
    selectToolConfirmations([call({}, { arguments: '<script>invalid JSON' })], 'chat')[0]
      .argumentsText,
    '<script>invalid JSON',
  );
});

test('trace replay dedupes per call, preserves sequence and keeps multiple confirmations', () => {
  const first = call({ sequence: 2 });
  const second = call({ id: 'second', sequence: 3 }, { call_id: 'second' });
  const replay = call({ id: 'replay', sequence: 4 });
  assert.deepEqual(
    selectToolConfirmations([second, replay, first], 'chat').map((item) => item.callId),
    ['call', 'second'],
  );
  assert.equal(
    selectToolConfirmations([call({}, { call_id: undefined })], 'chat')[0].callId,
    'source',
  );
});

test('actual output resolves only its chat/call, never matching Message.id or waiting for completion', () => {
  const output = call(
    { id: 'output-id', type: 'plugin_call_output', status: 'in_progress' },
    { output: 'done' },
  );
  assert.equal(selectToolConfirmations([call(), output], 'chat').length, 0);
  assert.equal(
    selectToolConfirmations([call(), { ...output, chat_id: 'other' }], 'chat').length,
    1,
  );
  const mismatched = call(
    { id: 'message-id', type: 'plugin_call_output' },
    { call_id: 'unrelated' },
  );
  assert.equal(selectToolConfirmations([call(), mismatched], 'chat').length, 1);
  const fallback = call({ type: 'plugin_call_output', source_id: 'call', content: [] });
  assert.equal(selectToolConfirmations([call(), fallback], 'chat').length, 0);
});

test('empty output start and identity-only envelopes keep approval visible until actual output or terminal result', () => {
  const start = call({
    id: 'out',
    type: 'plugin_call_output',
    status: 'in_progress',
    source_id: 'call',
    content: [],
  });
  const envelope = call(
    { id: 'out', type: 'plugin_call_output', status: 'in_progress' },
    { output: '' },
  );
  for (const placeholder of [start, envelope]) {
    assert.equal(selectToolConfirmations([call(), placeholder], 'chat').length, 1);
  }
  for (const status of ['completed', 'failed', 'cancelled'] as const) {
    assert.equal(selectToolConfirmations([call(), { ...start, status }], 'chat').length, 0);
  }
});

test('readiness, read-only, terminal, stopping and exact deadline all prevent submitting', () => {
  const candidate = selectToolConfirmations([call()], 'chat')[0];
  const context = { status: 'running' as const, readOnly: false, stopping: false, ready: true };
  const deadline = Date.parse(candidate.expiresAt!);
  assert.equal(toolConfirmationAvailability(candidate, context, deadline - 1), 'pending');
  assert.equal(toolConfirmationAvailability(candidate, context, deadline), 'expired');
  assert.equal(
    toolConfirmationAvailability(candidate, { ...context, ready: false }, deadline - 1),
    'loading',
  );
  assert.equal(
    toolConfirmationAvailability(candidate, { ...context, readOnly: true }, deadline - 1),
    'readOnly',
  );
  assert.equal(
    toolConfirmationAvailability(candidate, { ...context, status: 'completed' }, deadline - 1),
    'ended',
  );
  assert.equal(
    toolConfirmationAvailability(candidate, { ...context, stopping: true }, deadline - 1),
    'ended',
  );
  assert.equal(
    toolConfirmationAvailability({ ...candidate, expiresAt: 'bad date' }, context),
    'unavailable',
  );
});

test('clarification and tool confirmation coexist without one claiming the other', () => {
  const question = call(
    { id: 'question' },
    {
      name: 'ask_user_question',
      call_id: 'question',
      arguments: {
        title: 'Choose',
        questions: [{ question: 'Which?', multiSelect: false, options: [{ label: 'A' }] }],
      },
    },
  );
  const messages = [question, call()];
  assert.deepEqual(
    selectToolConfirmations(messages, 'chat').map((item) => item.callId),
    ['call'],
  );
  assert.equal(selectPendingAskUserQuestion(messages)?.id, 'question');
});

test('deadline removes the expired overlay on a clock tick and on trace reload, without hiding other calls', () => {
  const candidates = selectToolConfirmations(
    [
      call(),
      call(
        {
          id: 'later',
          sequence: 2,
          metadata: {
            awaiting_confirmation: true,
            expires_at: '2026-10-29T12:31:00Z',
          },
        },
        { call_id: 'later' },
      ),
    ],
    'chat',
  );
  const context = { status: 'running' as const, stopping: false };
  const deadline = Date.parse(candidates[0].expiresAt!);
  assert.equal(visibleToolConfirmations(candidates, context, deadline - 1).length, 2);
  assert.deepEqual(
    visibleToolConfirmations(candidates, context, deadline).map((item) => item.callId),
    ['later'],
  );
  assert.deepEqual(visibleToolConfirmations(candidates, context, deadline + 60_000), []);
  assert.deepEqual(
    visibleToolConfirmations(candidates, { ...context, stopping: true }, deadline - 1),
    [],
  );
  for (const status of ['completed', 'failed', 'canceled'] as const) {
    assert.deepEqual(
      visibleToolConfirmations(candidates, { ...context, status }, deadline - 1),
      [],
    );
  }
  // Presentation must not rewrite metadata or the underlying trace candidates.
  assert.equal(candidates.length, 2);
  assert.equal(candidates[0].expiresAt, '2026-10-29T12:30:00Z');
});
