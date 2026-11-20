import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat, ModelCallEvent } from '../../api/types.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { shouldForwardRuntimeStreamEvent } from '../runtime/runtimeSteer.js';

const event: ModelCallEvent = {
  object: 'model.call',
  session_id: 'session-1',
  chat_id: 'chat-1',
  sequence_number: 2,
  model_name: 'qwen-plus',
};

test('model.call advances the cursor and does not add a chat message', () => {
  const projection = projectStreamEvent(createChatStreamProjection(chat()), event);
  assert.equal(projection.messages.length, 0);
  assert.equal(projection.lastSequenceNumber, 2);
  assert.equal(projection.chat.status, 'running');
  assert.equal(shouldForwardRuntimeStreamEvent(event, new Set()), false);
});

function chat(): ConsoleChat {
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
  };
}
