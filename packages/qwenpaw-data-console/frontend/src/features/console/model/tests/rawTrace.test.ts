import assert from 'node:assert/strict';
import test from 'node:test';
import type { MessageContent, TraceMessage } from '../../api/types.js';
import { isRawTraceItemFailed, selectRawTraceItems } from '../runtime/rawTrace.js';

function content(data: unknown, overrides: Partial<MessageContent> = {}): MessageContent {
  return {
    object: 'content',
    type: 'data',
    delta: false,
    index: 0,
    status: 'completed',
    msg_id: null,
    text: null,
    image_url: null,
    data,
    format: null,
    video_url: null,
    filename: null,
    file_url: null,
    refusal: null,
    ...overrides,
  };
}

function message(overrides: Partial<TraceMessage> = {}): TraceMessage {
  return {
    id: 'input-1',
    object: 'message',
    chat_id: 'chat-1',
    sequence: 1,
    type: 'plugin_call',
    role: 'assistant',
    content: [content({ call_id: 'call-1', name: 'query' })],
    status: 'completed',
    metadata: null,
    ...overrides,
  };
}

function output(state: unknown, overrides: Partial<TraceMessage> = {}): TraceMessage {
  return message({
    id: 'output-1',
    sequence: 2,
    type: 'plugin_call_output',
    role: 'tool',
    content: [content({ call_id: 'call-1', state })],
    ...overrides,
  });
}

test('failed standalone messages are marked regardless of message type', () => {
  for (const type of ['message', 'reasoning', 'plugin_call', 'plugin_call_output']) {
    const [item] = selectRawTraceItems([message({ type, status: 'failed' })]);
    assert.equal(isRawTraceItemFailed(item), true, type);
  }
});

test('paired items inspect both input and output message status', () => {
  for (const failedSide of ['input', 'output']) {
    const input = message({ status: failedSide === 'input' ? 'failed' : 'completed' });
    const result = output('success', {
      status: failedSide === 'output' ? 'failed' : 'completed',
    });
    const items = selectRawTraceItems([input, result]);
    assert.equal(items.length, 1);
    assert.equal(items[0].input, input);
    assert.equal(items[0].output, result);
    assert.equal(isRawTraceItemFailed(items[0]), true, failedSide);
  }
});

for (const state of ['error', 'interrupted', 'denied']) {
  test(`completed tool output with ${state} marks paired and orphan items as failed`, () => {
    const result = output(state);
    for (const messages of [[message(), result], [result]]) {
      const [item] = selectRawTraceItems(messages);
      assert.equal(isRawTraceItemFailed(item), true);
    }
  });
}

test('non-failed, missing and unknown message or tool states do not imply failure', () => {
  const statuses = ['created', 'in_progress', 'completed', 'cancelled', undefined, 'unknown'];
  for (const status of statuses) {
    for (const state of ['success', 'running', 'unknown', undefined, null, {}, []]) {
      const result = output(state, { status: status as TraceMessage['status'] });
      const [item] = selectRawTraceItems([result]);
      assert.equal(isRawTraceItemFailed(item), false, `${status}/${String(state)}`);
    }
  }
  const [cancelledWithError] = selectRawTraceItems([output('error', { status: 'cancelled' })]);
  assert.equal(isRawTraceItemFailed(cancelledWithError), true);
});

test('missing or malformed data and error text are not treated as failure evidence', () => {
  const blocks = [
    ...[undefined, null, [], [{ state: 'error' }], 42, 'error', '{"state":"error"}', {}].map(
      (data) => content(data),
    ),
    content({ output: 'error: connection failed' }),
    content({ state: 'error' }, { type: 'text', text: 'error' }),
  ];
  for (const contents of [[], ...blocks.map((block) => [block]), blocks]) {
    const [item] = selectRawTraceItems([output(undefined, { content: contents })]);
    assert.equal(isRawTraceItemFailed(item), false);
  }
});

test('only tool output data states imply failure, and all its data blocks are checked', () => {
  for (const type of ['message', 'reasoning', 'plugin_call']) {
    const [item] = selectRawTraceItems([message({ type, content: [content({ state: 'error' })] })]);
    assert.equal(isRawTraceItemFailed(item), false, type);
  }
  const [item] = selectRawTraceItems([
    output('success', {
      content: [content(null), content({ state: 'success' }), content({ state: 'denied' })],
    }),
  ]);
  assert.equal(isRawTraceItemFailed(item), true);
});

test('failure is derived from the latest item as output arrives or status changes', () => {
  const input = message();
  const snapshots = [
    [input],
    [input, output(undefined, { status: 'in_progress' })],
    [input, output('error')],
    [input, output('success', { status: 'failed' })],
  ];
  assert.deepEqual(
    snapshots.map((messages) => {
      const [item] = selectRawTraceItems(messages);
      assert.equal(item.id, input.id);
      return isRawTraceItemFailed(item);
    }),
    [false, false, true, true],
  );
});
