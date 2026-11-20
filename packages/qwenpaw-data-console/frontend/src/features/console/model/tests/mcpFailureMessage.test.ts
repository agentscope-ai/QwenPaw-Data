import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat, McpConnectEvent } from '../../api/types.js';
import { createMcpFailureMessage } from '../chat/mcpFailureMessage.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { hydrateProjectionTrace } from '../chat/sessionHistory.js';
import { selectChatTimeline } from '../runtime/selectChatTimeline.js';

const event: McpConnectEvent = {
  object: 'mcp.connect',
  session_id: 'session-1',
  chat_id: 'chat-1',
  sequence_number: 2,
  mcp_name: 'test-server',
  status: 'failed',
  error: 'Connection refused',
};

test('only failures become failed tool cards in event order after the user request', () => {
  const before = createChatStreamProjection(chat());
  const success = projectStreamEvent(before, { ...event, status: 'connected' });
  assert.equal(success.messages.length, 0);
  const first = projectStreamEvent(before, event);
  const second = projectStreamEvent(first, { ...event, sequence_number: 3, mcp_name: 'second' });
  assert.equal(second.messages.length, 2);
  assert.equal(second.messages[0].type, 'plugin_call_output');
  assert.equal(second.messages[0].status, 'failed');
  assert.equal(second.chat.status, 'running');
  const timeline = selectChatTimeline(second);
  assert.deepEqual(
    timeline.map((item) => item.kind),
    ['user', 'raw', 'raw'],
  );
  assert.ok(timeline[1].seq < timeline[2].seq);
  assert.ok(timeline[2].seq < 1);
});

test('replay and foreign chat/session events cannot duplicate or leak cards', () => {
  const first = projectStreamEvent(createChatStreamProjection(chat()), event);
  assert.equal(projectStreamEvent(first, event), first);
  for (const change of [{ chat_id: 'other' }, { session_id: 'other' }]) {
    assert.equal(projectStreamEvent(first, { ...event, sequence_number: 4, ...change }), first);
  }
  for (const change of [
    { status: 'connected' as const },
    { mcp_name: '' },
    { sequence_number: NaN },
  ]) {
    assert.equal(createMcpFailureMessage({ ...event, ...change }), null);
  }
});

test('trace hydration restores failure cards and merges live copies without duplicates', () => {
  const message = createMcpFailureMessage(event)!;
  const snapshot = createChatStreamProjection(chat({ last_sequence_number: 10 }));
  const history = hydrateProjectionTrace(snapshot, [message]);
  assert.equal(history.messages.length, 1);
  assert.equal(history.lastSequenceNumber, 10);
  const live = projectStreamEvent(createChatStreamProjection(chat()), event);
  assert.equal(hydrateProjectionTrace(live, [message]).messages.length, 1);
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
