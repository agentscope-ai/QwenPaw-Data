import assert from 'node:assert/strict';
import test from 'node:test';
import type {
  BizEvent,
  ConsoleChat,
  ConsoleStreamEvent,
  Segment,
  TimingEvent,
  TraceMessage,
} from '../../api/types.js';
import {
  durationSeconds,
  formatExecutionSeconds,
  indexExecutionTimings,
  mergeTimingEvents,
  segmentDurationParts,
  otherDurationSeconds,
} from '../runtime/executionTiming.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import { hydrateProjectionTrace } from '../chat/sessionHistory.js';
import { selectChatTimeline } from '../runtime/selectChatTimeline.js';
import { segmentDurationSeconds } from '../runtime/runtimeProjection.js';
import { shouldForwardRuntimeStreamEvent } from '../runtime/runtimeSteer.js';

const epoch = Date.parse('2026-11-04T08:00:00Z') / 1000;
const event = (
  object: string,
  seq: number,
  seconds: number,
  extra: Partial<TimingEvent> = {},
): TimingEvent => ({
  object,
  sequence_number: seq,
  created_at: new Date((epoch + seconds) * 1000).toISOString(),
  session_id: 's',
  chat_id: 'c',
  ...extra,
});
const message = (seq: number, seconds: number, block: string) =>
  event('message', seq, seconds, {
    id: `message-${block}`,
    type: 'message',
    role: 'assistant',
    source_id: block,
  });
const chat = (extra: Partial<ConsoleChat> = {}): ConsoleChat => ({
  id: 'c',
  session_id: 's',
  sequence: 1,
  user_input: '2+2',
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
  ...extra,
});

test('model wait is counted, rounds are isolated, and a completion after model.end stays linked', () => {
  const timings = indexExecutionTimings([
    event('model.call', 1, 0),
    event('model.call.end', 2, 3),
    message(3, 3.1, 'a'),
    event('model.call', 4, 5),
    message(5, 6, 'b'),
    event('model.call.end', 6, 7),
  ]);
  assert.equal(durationSeconds(timings.models.get('a')!), 3);
  assert.equal(durationSeconds(timings.models.get('b')!), 2);
});

test('parallel tools are paired by call_id, not arrival order or tool name', () => {
  const timings = indexExecutionTimings([
    event('tool.result.start', 1, 0, { call_id: 'a' }),
    event('tool.result.start', 2, 1, { call_id: 'b' }),
    event('tool.result.end', 3, 2, { call_id: 'b' }),
    event('tool.result.end', 4, 4, { call_id: 'a' }),
  ]);
  assert.equal(durationSeconds(timings.tools.get('a')!), 4);
  assert.equal(durationSeconds(timings.tools.get('b')!), 1);
});

test('thinking owns the model label across raw/business cards without hiding another call', () => {
  const records = [
    event('model.call', 1, 0),
    { ...message(2, 1, 'thinking'), type: 'reasoning' },
    message(3, 2, 'reply'),
    event('model.call.end', 4, 3),
    event('model.call', 5, 4),
    message(6, 5, 'reply-only'),
    event('model.call.end', 7, 7),
  ];
  const business = (block: string, seq: number, thinking = false): BizEvent => ({
    event_id: block,
    block_id: block,
    seq,
    channel: 'main',
    status: 'done',
    started_at: epoch,
    ended_at: epoch + 3,
    presentation: { card_type: thinking ? 'thinking' : 'text', caption: block, body: block },
  });
  const raw = (block: string, sequence: number, thinking = false): TraceMessage => ({
    id: block,
    chat_id: 'c',
    metadata: null,
    source_id: block,
    object: 'message',
    sequence,
    type: thinking ? 'reasoning' : 'message',
    role: 'assistant',
    status: 'completed',
    content: [
      {
        object: 'content',
        type: 'text',
        text: block,
        delta: false,
        index: 0,
        status: 'completed',
        msg_id: block,
        image_url: null,
        data: null,
        format: null,
        video_url: null,
        filename: null,
        file_url: null,
        refusal: null,
      },
    ],
  });
  for (const mode of ['raw', 'business', 'mixed']) {
    const messages = mode === 'business' ? [] : [raw('thinking', 1, true)];
    const biz = mode === 'raw' ? [] : [business('reply', 2), business('reply-only', 3)];
    if (mode === 'raw') messages.push(raw('reply', 2), raw('reply-only', 3));
    if (mode === 'business') biz.unshift(business('thinking', 1, true));
    const projection = {
      ...createChatStreamProjection(chat({ biz_events: biz }), messages),
      timingEvents: records,
    };
    const timeline = selectChatTimeline(projection);
    const timingFor = (id: string) => {
      const item = timeline.find((item) => item.id === id);
      return item?.kind === 'raw'
        ? item.message.timing
        : item?.kind === 'biz_event'
          ? item.event.timing
          : undefined;
    };
    assert.equal(durationSeconds(timingFor('thinking')!), 3, mode);
    assert.equal(timingFor('reply'), undefined, mode);
    assert.equal(durationSeconds(timingFor('reply-only')!), 3, mode);
    // Label suppression must not remove the reply's timing from segment accounting.
    assert.equal(durationSeconds(indexExecutionTimings(records).models.get('reply')!), 3);
  }
});

test('incomplete, old, invalid and reversed timestamps do not invent durations', () => {
  for (const start of [undefined, 'bad date', new Date((epoch + 10) * 1000).toISOString()]) {
    const timings = indexExecutionTimings([
      event('model.call', 1, 0, { created_at: start }),
      message(2, 1, 'a'),
      event('model.call.end', 3, 2),
      event('tool.result.end', 4, 3, { call_id: 'b' }),
      event('tool.result.start', 5, 4, { call_id: 'c' }),
    ]);
    assert.equal(timings.models.size, 0);
    assert.equal(timings.tools.size, 0);
  }
  assert.equal(formatExecutionSeconds(0.001), '<0.1');
  assert.equal(formatExecutionSeconds(0.23), '0.2');
  assert.equal(formatExecutionSeconds(2), '2');
  assert.deepEqual(segmentDurationParts(59.99), { minutes: 1, seconds: '0' });
  assert.deepEqual(segmentDurationParts(0.03), { minutes: 0, seconds: '<0.1' });
});

test('HTTP replay ahead of the SSE cursor stays ordered as live events catch up', () => {
  const records = [event('model.call', 1, 0), message(2, 2, 'a'), event('model.call.end', 3, 3)];
  let projection = hydrateProjectionTrace(createChatStreamProjection(chat()), [], records);
  projection = projectStreamEvent(projection, {
    ...records[0],
    model_name: 'qwen',
  } as ConsoleStreamEvent);
  projection = projectStreamEvent(projection, {
    ...records[2],
    input_tokens: 1,
    output_tokens: 1,
  } as ConsoleStreamEvent);
  assert.deepEqual(
    projection.timingEvents?.map((item) => item.sequence_number),
    [1, 2, 3],
  );
  assert.equal(
    durationSeconds(indexExecutionTimings(projection.timingEvents!).models.get('a')!),
    3,
  );
});

test('SSE timing advances the cursor without SDK messages; late hydration cannot rewind it', () => {
  const start = { ...event('model.call', 1, 0), model_name: 'qwen' } as ConsoleStreamEvent;
  const end = {
    ...event('model.call.end', 3, 3),
    input_tokens: 1,
    output_tokens: 1,
  } as ConsoleStreamEvent;
  let projection = createChatStreamProjection(chat());
  for (const item of [start, end]) {
    projection = projectStreamEvent(projection, item);
    assert.equal(shouldForwardRuntimeStreamEvent(item, new Set()), false);
  }
  assert.equal(projectStreamEvent(projection, start), projection);
  const hydrated = hydrateProjectionTrace(
    projection,
    [],
    [start, message(2, 2, 'a'), { ...end, created_at: 'bad' }],
  );
  assert.equal(hydrated.lastSequenceNumber, 3);
  assert.equal(hydrated.chat.status, 'running');
  assert.equal(hydrated.messages.length, 0);
  assert.equal(durationSeconds(indexExecutionTimings(hydrated.timingEvents!).models.get('a')!), 3);
  assert.equal(
    mergeTimingEvents(
      [],
      [
        { ...start, session_id: 'other' },
        { ...start, chat_id: 'other' },
      ],
      projection.chat,
    ).length,
    0,
  );
});

test('live and refreshed segments/cards show the same model duration despite unrelated sequence domains', () => {
  const business: BizEvent = {
    event_id: 'biz-a',
    block_id: 'a',
    seq: 900,
    channel: 'main',
    status: 'done',
    started_at: epoch,
    ended_at: epoch + 5,
    presentation: { card_type: 'text', caption: '模型回复', body: '4' },
  };
  const segment: Segment = {
    segment_id: 'seg-1',
    chat_id: 'c',
    title: '结果',
    input: null,
    behavior: '',
    conclusion: '4',
    artifact: null,
    coverage: { start_seq: 900, end_seq: 900 },
    started_at: epoch + 2.75,
    ended_at: epoch + 3,
  };
  const records = [event('model.call', 1, 0), message(2, 2.9, 'a'), event('model.call.end', 3, 3)];
  const base = chat({
    status: 'completed',
    segments: [segment],
    biz_events: [business],
    last_sequence_number: 20,
  });
  const live = { ...createChatStreamProjection(base), timingEvents: records };
  const restored = hydrateProjectionTrace(createChatStreamProjection(base), [], records);
  assert.deepEqual(selectChatTimeline(live), selectChatTimeline(restored));
  const item = selectChatTimeline(restored).find((item) => item.kind === 'segment');
  assert.ok(item?.kind === 'segment');
  assert.equal(segmentDurationSeconds(item.segment), 0.25);
  assert.equal(item.segment.workedForSeconds, 5);
  assert.equal(otherDurationSeconds(item.segment.workedForSeconds!, item.segment.timingTotals), 2);
  assert.equal(durationSeconds(item.events[0].timing!), 3);
  assert.deepEqual(item.segment.timingTotals, { modelSeconds: 3, toolSeconds: 0 });
  assert.equal(base.segments[0].workedForSeconds, undefined);
});

test('segment totals deduplicate calls, sum parallel tools, and exclude other segments', () => {
  const records = [
    event('model.call', 1, 0),
    { ...message(2, 1, 'thinking'), type: 'reasoning' },
    message(3, 2, 'reply'),
    event('model.call.end', 4, 3),
    event('tool.result.start', 5, 3, { call_id: 'a' }),
    event('tool.result.start', 6, 3, { call_id: 'b' }),
    event('tool.result.end', 7, 33, { call_id: 'a' }),
    event('tool.result.end', 8, 38, { call_id: 'b' }),
    event('model.call', 9, 38),
    message(10, 39, 'second'),
    event('model.call.end', 11, 41),
    event('model.call', 12, 41),
    message(13, 42, 'outside'),
    event('model.call.end', 14, 50),
  ];
  const blocks = ['thinking', 'reply', 'a', 'a', 'b', 'b', 'second', 'outside'];
  const biz = blocks.map<BizEvent>((block, index) => ({
    event_id: `biz-${index}`,
    block_id: block,
    seq: index + 1,
    channel: 'main',
    status: 'done',
    started_at: epoch,
    ended_at: epoch + 41,
    presentation: {
      card_type: block === 'thinking' ? 'thinking' : ['a', 'b'].includes(block) ? 'tool' : 'text',
      caption: block,
      body: block,
    },
  }));
  const segment: Segment = {
    segment_id: 'seg',
    chat_id: 'c',
    title: 'summary',
    input: null,
    behavior: '',
    conclusion: '',
    artifact: null,
    coverage: { start_seq: 1, end_seq: 7 },
    started_at: epoch,
    ended_at: epoch + 41,
  };
  const projection = {
    ...createChatStreamProjection(chat({ biz_events: biz, segments: [segment] })),
    timingEvents: records,
  };
  const item = selectChatTimeline(projection).find((entry) => entry.kind === 'segment');
  assert.ok(item?.kind === 'segment');
  assert.deepEqual(item.segment.timingTotals, { modelSeconds: 6, toolSeconds: 65 });
  assert.equal(segmentDurationSeconds(item.segment), 41);
  assert.equal(item.segment.workedForSeconds, 41);
  assert.equal(otherDurationSeconds(item.segment.workedForSeconds!, item.segment.timingTotals), 0);
  assert.equal(item.events.find((entry) => entry.block_id === 'reply')?.timing, undefined);
  assert.equal(segment.timingTotals, undefined);
  const historical = hydrateProjectionTrace(
    createChatStreamProjection(projection.chat),
    [],
    records,
  );
  assert.deepEqual(selectChatTimeline(historical), selectChatTimeline(projection));
  const legacy = selectChatTimeline(createChatStreamProjection(projection.chat)).find(
    (entry) => entry.kind === 'segment',
  );
  assert.ok(legacy?.kind === 'segment');
  assert.equal(legacy.segment.timingTotals, undefined);
  assert.equal(legacy.segment.workedForSeconds, 41);
  assert.equal(otherDurationSeconds(41, legacy.segment.timingTotals), null);
  assert.equal(otherDurationSeconds(null, item.segment.timingTotals), null);
});
