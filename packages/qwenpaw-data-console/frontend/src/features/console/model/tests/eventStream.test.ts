import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat, ConsoleStreamEvent, TraceMessage } from '../../api/types.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { shouldForwardRuntimeStreamEvent } from '../runtime/runtimeSteer.js';
import { selectChatTimeline } from '../runtime/selectChatTimeline.js';
import { selectRawTraceItems } from '../runtime/rawTrace.js';
import { indexExecutionTimings, toTimingEvent } from '../runtime/executionTiming.js';
import { hydrateProjectionTrace } from '../chat/sessionHistory.js';

const child = { type: 'subagent', parent_call_id: 'spawn-1' } as const;
const base = { session_id: 'session-1', chat_id: 'chat-1' };
function chat(): ConsoleChat {
  return {
    id: 'chat-1',
    session_id: 'session-1',
    sequence: 1,
    user_input: '分析',
    datasource_id: null,
    kind: 'simple',
    forkable: false,
    status: 'running',
    last_sequence_number: 0,
    started_at: null,
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
function response(
  sequence_number: number,
  status: 'in_progress' | 'completed' | 'failed' | 'cancelled',
): ConsoleStreamEvent {
  return {
    ...base,
    sequence_number,
    object: 'response',
    id: 'main-response',
    status,
    usage: null,
    error: status === 'failed' ? { code: 'INTERNAL', message: 'child failed' } : null,
  };
}
function message(id: string, sequence: number, stream: TraceMessage['stream']): TraceMessage {
  return {
    id,
    sequence,
    stream,
    object: 'message',
    chat_id: 'chat-1',
    type: 'message',
    role: 'assistant',
    content: [],
    status: 'in_progress',
    metadata: null,
  };
}

test('child response terminals advance the shared cursor without ending or failing the main chat', () => {
  let state = projectStreamEvent(createChatStreamProjection(chat()), response(1, 'in_progress'));
  for (const status of ['completed', 'failed', 'cancelled'] as const) {
    const event = { ...response(state.lastSequenceNumber + 1, status), stream: child };
    const previous = state;
    state = projectStreamEvent(state, event);
    assert.equal(state.responseId, 'main-response');
    assert.equal(state.responseStatus, 'in_progress');
    assert.equal(state.chat.status, 'running');
    assert.equal(state.chat.error, null);
    assert.equal(state.streamError, null);
    assert.equal(state.chat.forkable, false);
    assert.equal(state.lastSequenceNumber, event.sequence_number);
    assert.equal(state.chat.last_sequence_number, event.sequence_number);
    assert.equal(shouldForwardRuntimeStreamEvent(event, new Set()), false);
    assert.equal(projectStreamEvent(state, event), state);
    assert.equal(projectStreamEvent(previous, { ...event, chat_id: 'other' }), previous);
  }
  state = projectStreamEvent(state, { ...response(5, 'completed'), stream: { type: 'main' } });
  assert.equal(state.chat.status, 'completed');
  assert.equal(state.chat.forkable, true);
});

test('child messages and deltas remain in raw trace, outside main reply and SDK output', () => {
  const event: ConsoleStreamEvent = {
    ...message('child-msg', 1, child),
    ...base,
    sequence_number: 1,
  };
  let state = projectStreamEvent(createChatStreamProjection(chat()), event);
  const delta: ConsoleStreamEvent = {
    ...base,
    sequence_number: 2,
    stream: child,
    object: 'content',
    msg_id: 'child-msg',
    type: 'text',
    index: 0,
    delta: true,
    text: 'child output',
    status: 'in_progress',
    image_url: null,
    data: null,
    format: null,
    video_url: null,
    filename: null,
    file_url: null,
    refusal: null,
  };
  state = projectStreamEvent(state, delta);
  assert.equal(state.messages[0].content[0].text, 'child output');
  assert.deepEqual(state.messages[0].stream, child);
  const card = selectChatTimeline(state, { includeSubagents: true }).find(
    (item) => item.kind === 'subagent',
  );
  assert.ok(card?.kind === 'subagent');
  assert.equal(card.activity.messages[0].content[0].text, 'child output');
  assert.equal(card.activity.status, 'running');
  assert.equal(selectRawTraceItems(state.messages).length, 1);
  assert.equal(
    selectChatTimeline(state).some((item) => item.kind === 'raw'),
    false,
  );
  assert.equal(shouldForwardRuntimeStreamEvent(event, new Set()), false);
  assert.equal(shouldForwardRuntimeStreamEvent(delta, new Set()), false);
  const wrongStream = projectStreamEvent(state, {
    ...delta,
    sequence_number: 3,
    stream: { type: 'main' },
  });
  assert.equal(wrongStream.messages[0].content[0].text, 'child output');
});

test('parallel subagents stay attached to their own parent tools when a segment covers them', () => {
  const second = { type: 'subagent', parent_call_id: 'spawn-2' } as const;
  const parent = {
    ...message('parent', 1, undefined),
    type: 'plugin_call',
    source_id: 'spawn-1',
  };
  const anotherParent = { ...parent, id: 'other-parent', sequence: 2, source_id: 'spawn-2' };
  const main = chat();
  main.biz_events = [
    {
      event_id: 'parent-biz',
      seq: 1,
      channel: 'main',
      block_id: 'spawn-1',
      status: 'done',
      presentation: { card_type: 'tool', caption: 'Spawn subagent', body: '' },
      started_at: 0,
      ended_at: 1,
    },
  ];
  main.segments = [
    {
      segment_id: 'summary',
      chat_id: main.id,
      title: 'Summary',
      input: null,
      behavior: '',
      conclusion: '',
      artifact: null,
      coverage: { start_seq: 1, end_seq: 1 },
      started_at: 0,
      ended_at: 1,
    },
  ];
  const state = projectStreamEvent(
    createChatStreamProjection(main, [
      parent,
      anotherParent,
      message('child-2', 1, second),
      message('child-1', 1, child),
    ]),
    { ...response(3, 'completed'), stream: second },
  );
  const timeline = selectChatTimeline(state, { includeSubagents: true });
  assert.deepEqual(
    timeline.map((item) => item.id),
    [
      'user-chat-1',
      'parent-biz',
      'subagent-spawn-1',
      'summary',
      'other-parent',
      'subagent-spawn-2',
    ],
  );
  const cards = timeline.filter((item) => item.kind === 'subagent');
  assert.deepEqual(
    cards.map(({ activity }) => [
      activity.parentCallId,
      activity.status,
      activity.messages.map(({ id }) => id),
    ]),
    [
      ['spawn-1', 'running', ['child-1']],
      ['spawn-2', 'completed', ['child-2']],
    ],
  );
  assert.deepEqual(timeline.find((item) => item.kind === 'segment')?.events, []);
  assert.equal(state.chat.status, 'running');
});

test('legacy and explicit main messages still enter the main timeline and SDK', () => {
  for (const stream of [undefined, { type: 'main' } as const]) {
    const event: ConsoleStreamEvent = {
      ...message('main-msg', 1, stream),
      ...base,
      sequence_number: 1,
    };
    const state = projectStreamEvent(createChatStreamProjection(chat()), event);
    assert.equal(
      selectChatTimeline(state).some((item) => item.kind === 'raw'),
      true,
    );
    assert.equal(shouldForwardRuntimeStreamEvent(event, new Set()), true);
  }
});

test('child context, error, summary and follow-up do not overwrite main state', () => {
  const events: ConsoleStreamEvent[] = [
    {
      ...base,
      sequence_number: 1,
      stream: child,
      object: 'context.usage',
      context_size: 100,
      used_tokens: 90,
      trigger_ratio: 0.8,
      reserve_ratio: 0.2,
      tool_result_limit: 100,
    },
    {
      ...base,
      sequence_number: 2,
      stream: child,
      object: 'error',
      code: 'CONFLICT',
      message: 'child error',
      details: null,
    },
    {
      ...base,
      sequence_number: 3,
      stream: child,
      object: 'followup.generated',
      followup: { chat_id: 'chat-1', questions: ['child question'] },
    },
    {
      ...base,
      sequence_number: 4,
      stream: child,
      object: 'biz_event',
      biz_event: {
        event_id: 'child-biz',
        seq: 1,
        channel: 'main',
        block_id: null,
        status: 'done',
        presentation: { card_type: 'text', caption: 'child', body: 'child output' },
        started_at: 0,
        ended_at: null,
      },
    },
  ];
  const state = events.reduce(projectStreamEvent, createChatStreamProjection(chat()));
  assert.equal(state.contextUsage, null);
  assert.equal(state.streamError, null);
  assert.equal(state.chat.followup, null);
  assert.deepEqual(state.chat.biz_events, []);
  assert.equal(state.lastSequenceNumber, 4);
});

test('tool calls from different streams never pair across agent boundaries', () => {
  const call = message('main-call', 1, { type: 'main' });
  call.type = 'plugin_call';
  call.content = [
    {
      object: 'content',
      type: 'data',
      data: { call_id: 'same-call' },
    } as TraceMessage['content'][number],
  ];
  const output = {
    ...message('child-output', 2, child),
    type: 'plugin_call_output',
    content: call.content,
  };
  assert.equal(selectRawTraceItems([call, output]).length, 2);
  const childCall = { ...call, id: 'child-call', stream: child };
  const items = selectRawTraceItems([call, childCall, output]);
  assert.equal(items.length, 2);
  assert.equal(items[0].output, null);
  assert.equal(items[1].output?.id, 'child-output');
});

test('interleaved child model timings cannot close the main model span', () => {
  const events: ConsoleStreamEvent[] = [
    {
      ...base,
      sequence_number: 1,
      object: 'model.call',
      model_name: 'main',
      created_at: '2026-11-10T00:00:00Z',
    },
    {
      ...base,
      sequence_number: 2,
      stream: child,
      object: 'model.call',
      model_name: 'child',
      created_at: '2026-11-10T00:00:01Z',
    },
    {
      ...base,
      sequence_number: 3,
      stream: child,
      object: 'model.call.end',
      input_tokens: 1,
      output_tokens: 1,
      created_at: '2026-11-10T00:00:02Z',
    },
    {
      ...message('main-msg', 4, undefined),
      ...base,
      sequence_number: 4,
      source_id: 'main-block',
      created_at: '2026-11-10T00:00:03Z',
    },
    {
      ...base,
      sequence_number: 5,
      object: 'model.call.end',
      input_tokens: 1,
      output_tokens: 1,
      created_at: '2026-11-10T00:00:05Z',
    },
  ];
  const records = events.flatMap((event) => {
    const record = toTimingEvent(event);
    return record ? [record] : [];
  });
  assert.deepEqual(records[1].stream, child);
  const span = indexExecutionTimings(records).models.get('main-block');
  assert.equal(span!.endedAt - span!.startedAt, 5);
});

test('history that retains stream provenance stays isolated during hydration', () => {
  const state = hydrateProjectionTrace(createChatStreamProjection(chat()), [
    message('child', 1, child),
    message('main', 2, undefined),
  ]);
  assert.equal(state.messages.length, 2);
  assert.deepEqual(
    selectChatTimeline(state)
      .filter((item) => item.kind === 'raw')
      .map((item) => item.id),
    ['main'],
  );
});

test('child PlanCreate cannot replace the main plan in live projection or history replay', () => {
  const call = { ...message('plan-call', 1, child), type: 'plugin_call', source_id: 'plan-1' };
  call.content = [
    {
      object: 'content',
      type: 'data',
      data: {
        name: 'PlanCreate',
        arguments: JSON.stringify({
          tasks: [{ id: 'child-task', subject: 'child task', description: 'child only' }],
        }),
      },
    } as TraceMessage['content'][number],
  ];
  const output = {
    ...message('plan-output', 2, child),
    type: 'plugin_call_output',
    source_id: 'plan-1',
  };
  output.content = [
    {
      object: 'content',
      type: 'data',
      data: { name: 'PlanCreate', state: 'success' },
    } as TraceMessage['content'][number],
  ];
  const replay = (messages: TraceMessage[]) =>
    messages.reduce(
      (state, item) =>
        projectStreamEvent(state, { ...item, ...base, sequence_number: item.sequence }),
      createChatStreamProjection(chat()),
    );
  assert.equal(replay([call, output]).chat.plan, null);
  assert.equal(createChatStreamProjection(chat(), [call, output]).chat.plan, null);
  const main = [call, output].map((item) => ({ ...item, stream: { type: 'main' } as const }));
  assert.equal(replay(main).chat.plan?.tasks[0].id, 'child-task');
  assert.equal(createChatStreamProjection(chat(), main).chat.plan?.tasks[0].id, 'child-task');
});
