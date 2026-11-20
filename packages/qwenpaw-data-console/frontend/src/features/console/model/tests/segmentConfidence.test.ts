import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat, Segment, SegmentConfidence } from '../../api/types';
import { parseSseFrame } from '../../api/sseFrames.js';
import { createChatStreamProjection, projectStreamEvent } from '../runtime/projectStream.js';
import {
  createSegmentRuntimeMessage,
  selectRuntimeProjectionPayload,
} from '../runtime/runtimeProjection.js';
import { selectChatTimeline } from '../runtime/selectChatTimeline.js';
import { readSegmentConfidence } from '../runtime/segmentConfidence.js';

const segment = (confidence?: SegmentConfidence | null): Segment => ({
  segment_id: 'seg-1',
  chat_id: 'chat-1',
  title: '核对数据',
  input: null,
  behavior: '核对口径',
  conclusion: '统计口径一致',
  artifact: null,
  coverage: { start_seq: 2, end_seq: 4 },
  started_at: null,
  ended_at: null,
  ...(confidence === undefined ? {} : { confidence }),
});
const chat = (segments: Segment[] = []): ConsoleChat => ({
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
  segments,
  biz_events: [],
  artifacts: [],
  followup: null,
});

function streamSegment(sequence: number, data: Segment) {
  const frame = parseSseFrame(
    `event: segment\nid: ${sequence}\ndata: ${JSON.stringify({
      object: 'segment',
      session_id: 'session-1',
      chat_id: 'chat-1',
      sequence_number: sequence,
      segment: data,
    })}`,
  );
  assert.ok(frame);
  return frame;
}

for (const level of ['high', 'medium', 'low'] as const) {
  test(`${level} confidence survives live SSE and snapshot through the runtime card payload`, () => {
    const confidence = { level, summary: `  ${'保留完整事实说明。'.repeat(40)}\n<示例文本>  ` };
    const data = segment(confidence);
    const history = createChatStreamProjection(
      JSON.parse(JSON.stringify(chat([data]))) as ConsoleChat,
    );
    const live = projectStreamEvent(createChatStreamProjection(chat()), streamSegment(1, data));
    for (const projection of [history, live]) {
      const item = selectChatTimeline(projection).find((entry) => entry.kind === 'segment');
      assert.ok(item?.kind === 'segment');
      const payload = selectRuntimeProjectionPayload(
        createSegmentRuntimeMessage(item.segment, item.events),
      );
      assert.ok(payload?.kind === 'segment');
      assert.deepEqual(readSegmentConfidence(payload.segment.confidence), confidence);
      assert.equal(payload.segment.conclusion, data.conclusion);
    }
  });
}

test('old snapshots and unscored live segments keep conclusions without a confidence badge', () => {
  for (const confidence of [undefined, null]) {
    const data = segment(confidence);
    const history = createChatStreamProjection(chat([data]));
    const live = projectStreamEvent(createChatStreamProjection(chat()), streamSegment(1, data));
    for (const projection of [history, live]) {
      assert.equal(readSegmentConfidence(projection.chat.segments[0].confidence), null);
      assert.equal(projection.chat.segments[0].conclusion, data.conclusion);
    }
  }
});

test('malformed confidence is hidden rather than treated as low or shown as an empty tooltip', () => {
  for (const value of [
    null,
    undefined,
    false,
    1,
    'high',
    [],
    {},
    { level: 'unknown', summary: '原因' },
    { level: 3, summary: '原因' },
    { level: 'high' },
    { level: 'high', summary: null },
    { level: 'high', summary: {} },
    { level: 'high', summary: '' },
    { level: 'high', summary: ' \n\t ' },
  ]) {
    assert.equal(readSegmentConfidence(value), null);
  }
});

test('new event cursor replaces confidence and can clear it without changing coverage order', () => {
  const initial = { level: 'high' as const, summary: '已核对' };
  const later = {
    ...segment(initial),
    segment_id: 'seg-2',
    coverage: { start_seq: 5, end_seq: 9 },
  };
  let projection = createChatStreamProjection(chat([later, segment(initial)]));
  const next = { level: 'medium' as const, summary: '缺少部分取数依据' };
  projection = projectStreamEvent(projection, streamSegment(1, segment(next)));
  assert.deepEqual(
    projection.chat.segments.map((item) => item.segment_id),
    ['seg-1', 'seg-2'],
  );
  assert.deepEqual(readSegmentConfidence(projection.chat.segments[0].confidence), next);
  const reasonOnly = { ...next, summary: '已补充依据，仍缺分组加总校验' };
  projection = projectStreamEvent(projection, streamSegment(2, segment(reasonOnly)));
  assert.deepEqual(readSegmentConfidence(projection.chat.segments[0].confidence), reasonOnly);
  // An older replay must not revert the latest reason.
  assert.equal(projectStreamEvent(projection, streamSegment(1, segment(initial))), projection);
  for (const [index, confidence] of [null, undefined].entries()) {
    projection = projectStreamEvent(projection, streamSegment(index + 3, segment(confidence)));
    assert.equal(readSegmentConfidence(projection.chat.segments[0].confidence), null);
    assert.deepEqual(projection.chat.segments[1], later);
  }
});
