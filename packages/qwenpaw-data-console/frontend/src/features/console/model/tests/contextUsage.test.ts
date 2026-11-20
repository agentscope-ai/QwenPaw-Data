import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat } from '../../api/types.js';
import { drainSseFrames } from '../../api/sseFrames.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import {
  contextCompressionProgress,
  contextUsagePercent,
  minimumTriggerPercent,
  selectLatestContextUsage,
} from '../runtime/contextUsage.js';

test('context usage survives chunked SSE and updates without adding a chat message', () => {
  const usage = {
    object: 'context.usage' as const,
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 1,
    context_size: 256000,
    used_tokens: 109900,
    trigger_ratio: 0.8,
    reserve_ratio: 0.1,
    tool_result_limit: 50000,
  };
  const wire = `data: ${JSON.stringify(usage)}\n\n`;
  const first = drainSseFrames(wire.slice(0, 45));
  assert.equal(first.events.length, 0);
  const events = drainSseFrames(first.remainder + wire.slice(45)).events;
  const projection = projectStreamEvent(createChatStreamProjection(chat()), events[0]);
  assert.equal(projection.contextUsage?.used_tokens, 109900);
  assert.equal(projection.messages.length, 0);
  assert.equal(projection.chat.status, 'running');
  assert.equal(projection.lastSequenceNumber, 1);
  // A reconnect duplicate or an event from a different session/chat must not replace usage.
  assert.equal(projectStreamEvent(projection, { ...usage, used_tokens: 1 }), projection);
  assert.equal(
    projectStreamEvent(projection, { ...usage, sequence_number: 2, session_id: 'other' }),
    projection,
  );
  assert.equal(
    projectStreamEvent(projection, { ...usage, sequence_number: 2, chat_id: 'other' }),
    projection,
  );
  // Compression can decrease usage; do not keep a high-water mark.
  const compressed = projectStreamEvent(projection, {
    ...usage,
    sequence_number: 2,
    used_tokens: 25000,
  });
  assert.equal(compressed.contextUsage?.used_tokens, 25000);
  const completed = projectStreamEvent(compressed, {
    object: 'response',
    id: 'response-1',
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 3,
    status: 'completed',
    usage: null,
    error: null,
  });
  assert.equal(completed.contextUsage?.used_tokens, 25000);
});

test('context usage rejects invalid payloads and keeps over-capacity token counts', () => {
  const state = createChatStreamProjection(chat());
  const usage = {
    object: 'context.usage' as const,
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 1,
    context_size: 1000,
    used_tokens: 1200,
    trigger_ratio: 0.8,
    reserve_ratio: 0.1,
    tool_result_limit: 50000,
  };
  for (const patch of [
    { context_size: 0 },
    { context_size: NaN },
    { used_tokens: -1 },
    { trigger_ratio: 2 },
    { reserve_ratio: 0.85 },
  ]) {
    assert.equal(projectStreamEvent(state, { ...usage, ...patch }).contextUsage, null);
  }
  const valid = projectStreamEvent(state, usage);
  assert.equal(contextUsagePercent(valid.contextUsage!), 120);
  assert.equal(minimumTriggerPercent(0.1), 11);
  assert.equal(minimumTriggerPercent(0.25), 26);
  assert.equal(minimumTriggerPercent(0.29), 30);
  assert.equal(minimumTriggerPercent(0.2899), 29);
});

test('compression ring fills at the trigger threshold while capacity usage stays independent', () => {
  const usage = {
    context_size: 1000,
    used_tokens: 400,
    trigger_ratio: 0.8,
    reserve_ratio: 0.1,
    tool_result_limit: 50000,
  };
  assert.equal(contextCompressionProgress(usage), 50);
  assert.equal(contextUsagePercent(usage), 40);
  assert.equal(contextCompressionProgress({ ...usage, used_tokens: 800 }), 100);
  assert.equal(contextCompressionProgress({ ...usage, used_tokens: 900 }), 100);
  assert.equal(contextCompressionProgress({ ...usage, used_tokens: 200 }), 25);
  assert.equal(contextCompressionProgress({ ...usage, trigger_ratio: 0.5 }), 80);
});

test('usage selection follows the latest chat with data within the selected session only', () => {
  const oldChat = createChatStreamProjection(chat());
  oldChat.contextUsage = {
    context_size: 1000,
    used_tokens: 300,
    trigger_ratio: 0.8,
    reserve_ratio: 0.1,
    tool_result_limit: 50000,
  };
  const newChat = createChatStreamProjection(chat({ id: 'chat-2', sequence: 2 }));
  newChat.contextUsage = { ...oldChat.contextUsage, used_tokens: 500 };
  assert.equal(selectLatestContextUsage([newChat, oldChat], 'session-1')?.used_tokens, 500);
  assert.equal(selectLatestContextUsage([newChat, oldChat], 'session-2'), null);
  assert.equal(selectLatestContextUsage([newChat, oldChat], undefined), null);
  assert.equal(createChatStreamProjection(chat()).contextUsage, null);
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
