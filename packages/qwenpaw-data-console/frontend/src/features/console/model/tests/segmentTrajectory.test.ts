import assert from 'node:assert/strict';
import test from 'node:test';
import type { BizEvent, ConsoleChat, Segment } from '../../api/types';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { selectChatTimeline, splitChatTimelineBySteer } from '../runtime/selectChatTimeline.js';
import {
  createSegmentRuntimeMessage,
  segmentDurationSeconds,
  eventDurationSeconds,
  selectRuntimeProjectionPayload,
} from '../runtime/runtimeProjection.js';
import { selectLastSessionSegmentKey } from '../runtime/segmentExpansion.js';
import {
  createRuntimeStreamBootstrapEvent,
  parseRuntimeStreamChunk,
} from '../runtime/runtimeMessages.js';

const event = (seq: number, overrides: Partial<BizEvent> = {}): BizEvent => ({
  event_id: `event-${seq}`,
  seq,
  channel: 'main',
  block_id: null,
  status: 'done',
  presentation: { card_type: 'text', caption: '模型回复', body: `reply ${seq}` },
  started_at: 100 + seq,
  ended_at: 102 + seq,
  ...overrides,
});
const segment = (overrides: Partial<Segment> = {}): Segment => ({
  segment_id: 'seg_0001',
  chat_id: 'chat-1',
  title: '结果',
  input: null,
  behavior: '执行',
  conclusion: '完成',
  artifact: null,
  coverage: { start_seq: 2, end_seq: 4 },
  started_at: 100,
  ended_at: 165,
  ...overrides,
});
const chat = (overrides: Partial<ConsoleChat> = {}): ConsoleChat => ({
  id: 'chat-1',
  session_id: 'session-1',
  sequence: 1,
  user_input: '分析',
  datasource_id: null,
  kind: 'simple',
  status: 'running',
  forkable: false,
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
  ...overrides,
});

test('refresh seeds a response identity that keeps snapshot and new segments renderable during SSE', () => {
  let projection = createChatStreamProjection(
    chat({
      last_sequence_number: 40,
      segments: [segment()],
      biz_events: [event(2)],
    }),
  );
  const bootstrap = createRuntimeStreamBootstrapEvent(projection.chat, projection.responseId);
  const response = parseRuntimeStreamChunk(JSON.stringify(bootstrap));
  assert.equal(response.object, 'response');
  assert.ok('id' in response);
  assert.equal(response.id, `response-${projection.chat.id}`);
  assert.equal(response.status, 'in_progress');
  // Initializing the SDK is not a new server event and cannot advance replay.
  assert.equal(projectStreamEvent(projection, bootstrap), projection);
  assert.equal(projection.lastSequenceNumber, 40);

  const nextSegment = segment({ segment_id: 'seg_0002', coverage: { start_seq: 5, end_seq: 6 } });
  projection = projectStreamEvent(projection, {
    object: 'segment',
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 41,
    segment: nextSegment,
  });
  // No response event is replayed before this increment. The seeded ID still
  // selects the projection rather than falling back to the SDK's raw output.
  assert.ok(
    projection.responseId === response.id || `response-${projection.chat.id}` === response.id,
  );
  assert.deepEqual(
    selectChatTimeline(projection)
      .filter((item) => item.kind === 'segment')
      .map((item) => item.id),
    ['seg_0001', 'seg_0002'],
  );

  projection = projectStreamEvent(projection, {
    ...bootstrap,
    id: 'server-response-1',
    sequence_number: 42,
    status: 'completed',
  });
  assert.equal(projection.responseId, 'server-response-1');
  assert.equal(projection.chat.status, 'completed');
  assert.equal(projection.chat.segments.length, 2);
});

test('reconnect within a mounted session preserves the known server response identity', () => {
  const projection = projectStreamEvent(createChatStreamProjection(chat()), {
    object: 'response',
    id: 'server-response-1',
    status: 'in_progress',
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 1,
    usage: null,
    error: null,
  });
  const bootstrap = createRuntimeStreamBootstrapEvent(projection.chat, projection.responseId);
  assert.equal(bootstrap.id, projection.responseId);
  assert.equal(bootstrap.sequence_number, 1);
  assert.equal(projectStreamEvent(projection, bootstrap), projection);
  assert.notEqual(
    createRuntimeStreamBootstrapEvent(chat({ id: 'chat-2' })).id,
    createRuntimeStreamBootstrapEvent(chat()).id,
  );
});

test('streaming events remain visible until the covering segment arrives', () => {
  let projection = createChatStreamProjection(
    chat({ biz_events: [event(1), event(2), event(4), event(5)] }),
  );
  assert.equal(
    selectChatTimeline(projection).filter((item) => item.kind === 'biz_event').length,
    4,
  );
  projection = projectStreamEvent(projection, {
    object: 'segment',
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 10,
    segment: segment(),
  });
  const timeline = selectChatTimeline(projection);
  assert.deepEqual(
    timeline.map((item) => item.id),
    ['user-chat-1', 'event-1', 'seg_0001', 'event-5'],
  );
  const summary = timeline.find((item) => item.kind === 'segment');
  assert.ok(summary?.kind === 'segment');
  assert.deepEqual(
    summary.events.map((item) => item.seq),
    [2, 4],
  );
  const payload = selectRuntimeProjectionPayload(
    createSegmentRuntimeMessage(summary.segment, summary.events),
  );
  assert.ok(payload?.kind === 'segment');
  assert.deepEqual(payload.events, summary.events);
});

test('history and live projection group the same events, including events arriving after the segment', () => {
  const source = chat({ segments: [segment()], biz_events: [event(2)] });
  let live = createChatStreamProjection(source);
  live = projectStreamEvent(live, {
    object: 'biz_event',
    session_id: 'session-1',
    chat_id: 'chat-1',
    sequence_number: 10,
    biz_event: event(3),
  });
  const history = createChatStreamProjection({ ...source, biz_events: [event(2), event(3)] });
  assert.deepEqual(selectChatTimeline(live), selectChatTimeline(history));
});

test('trajectory excludes user projections, invisible events, and subagents; Trace keeps covered events', () => {
  const projection = createChatStreamProjection(
    chat({
      segments: [segment({ coverage: { start_seq: 1, end_seq: 8 } })],
      biz_events: [
        event(1),
        event(2, { presentation: { card_type: 'user', caption: '', body: 'request' } }),
        event(3, { channel: 'subagent' }),
        event(4, { presentation: null }),
        event(5),
      ],
    }),
  );
  const timeline = selectChatTimeline(projection, { keepCoveredBizEvents: true });
  const summary = timeline.find((item) => item.kind === 'segment');
  assert.ok(summary?.kind === 'segment');
  assert.deepEqual(
    summary.events.map((item) => item.seq),
    [1, 5],
  );
  assert.deepEqual(
    timeline.filter((item) => item.kind === 'biz_event').map((item) => item.seq),
    [1, 2, 5],
  );
});

test('tool call/result are merged within the trajectory with error and full body preserved', () => {
  const tool = { card_type: 'tool' as const, caption: 'SQL', body: 'input' };
  const projection = createChatStreamProjection(
    chat({
      segments: [segment()],
      biz_events: [
        event(2, { block_id: 'tool-1', ended_at: null, presentation: tool }),
        event(3, {
          block_id: 'tool-1',
          status: 'error',
          presentation: { ...tool, body: 'output' },
        }),
        event(4),
      ],
    }),
  );
  const summary = selectChatTimeline(projection).find((item) => item.kind === 'segment');
  assert.ok(summary?.kind === 'segment');
  assert.equal(summary.events.length, 2);
  assert.equal(summary.events[0].presentation?.body, 'input\n\noutput');
  assert.equal(summary.events[0].status, 'error');
  assert.equal(summary.events[1].event_id, 'event-4');
});

test('new segment absorbs only its own range and keeps earlier trajectories intact', () => {
  const projection = createChatStreamProjection(
    chat({
      segments: [
        segment({ segment_id: 'seg_0002', coverage: { start_seq: 5, end_seq: 6 } }),
        segment(),
      ],
      biz_events: [event(6), event(4), event(2), event(5), event(7)],
    }),
  );
  const timeline = selectChatTimeline(projection);
  assert.deepEqual(
    timeline
      .filter((item) => item.kind === 'segment')
      .map((item) => item.events.map((entry) => entry.seq)),
    [
      [2, 4],
      [5, 6],
    ],
  );
  assert.deepEqual(
    timeline.filter((item) => item.kind === 'biz_event').map((item) => item.seq),
    [7],
  );
});

test('steer bubbles retain their position without duplicating the user projection in a trajectory', () => {
  const projection = createChatStreamProjection(
    chat({ segments: [segment()], biz_events: [event(2, { block_id: 'steer-source' }), event(4)] }),
    [
      {
        id: 'steer-1',
        chat_id: 'chat-1',
        sequence: 1,
        object: 'message',
        type: 'message',
        role: 'user',
        content: [],
        status: 'completed',
        source_id: 'steer-source',
        metadata: { source: 'steer' },
      },
    ],
  );
  const sections = splitChatTimelineBySteer(selectChatTimeline(projection));
  assert.equal(sections[0].kind, 'steer');
  const summary = selectChatTimeline(projection).find((item) => item.kind === 'segment');
  assert.ok(summary?.kind === 'segment');
  assert.deepEqual(
    summary.events.map((item) => item.seq),
    [4],
  );
});

test('only the latest session segment wins despite repeated IDs, unordered chats and a new empty chat', () => {
  const old = chat({ segments: [segment()] });
  const latest = chat({
    id: 'chat-2',
    sequence: 2,
    segments: [
      segment({ coverage: { start_seq: 5, end_seq: 8 } }),
      segment({ segment_id: 'earlier' }),
    ],
  });
  assert.equal(
    selectLastSessionSegmentKey([latest, old, chat({ id: 'chat-3', sequence: 3 })]),
    'chat-2:seg_0001',
  );
  assert.equal(selectLastSessionSegmentKey([old]), 'chat-1:seg_0001');
  assert.equal(selectLastSessionSegmentKey([]), null);
});

test('segment title duration uses only segment timestamps and preserves fractional seconds', () => {
  assert.equal(segmentDurationSeconds(segment()), 65);
  assert.equal(segmentDurationSeconds(segment({ started_at: 0, ended_at: 0 })), 0);
  assert.equal(segmentDurationSeconds(segment({ started_at: 0, ended_at: 1.25 })), 1.25);
  assert.equal(segmentDurationSeconds(segment({ started_at: null, ended_at: null })), null);
  assert.equal(segmentDurationSeconds(segment({ started_at: NaN, ended_at: Infinity })), null);
  assert.equal(segmentDurationSeconds(segment({ ended_at: 90 })), 0);
});

test('Worked for uses first and last event by sequence, including invisible boundaries', () => {
  const events = [
    event(4, { started_at: 108, ended_at: 110.5, presentation: null }),
    event(3, { started_at: 50, ended_at: 150 }),
    event(2, { started_at: 100, presentation: null }),
  ];
  assert.equal(eventDurationSeconds(events), 10.5);
  assert.deepEqual(
    events.map((item) => item.seq),
    [4, 3, 2],
  );
  const timeline = selectChatTimeline(
    createChatStreamProjection(
      chat({
        biz_events: events,
        segments: [segment({ coverage: { start_seq: 2, end_seq: 4 } })],
      }),
    ),
  );
  const item = timeline.find((entry) => entry.kind === 'segment');
  assert.ok(item?.kind === 'segment');
  assert.equal(item.segment.workedForSeconds, 10.5);
  assert.equal(segmentDurationSeconds(item.segment), 65);
  assert.equal(eventDurationSeconds([]), null);
  assert.equal(eventDurationSeconds([event(2, { ended_at: null })]), null);
  assert.equal(eventDurationSeconds([event(2, { started_at: NaN })]), null);
  assert.equal(eventDurationSeconds([event(2, { started_at: 0, ended_at: 0 })]), 0);
});
