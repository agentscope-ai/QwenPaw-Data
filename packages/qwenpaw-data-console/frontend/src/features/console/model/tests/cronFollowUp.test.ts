import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConsoleChat, MessageContent, TraceMessage } from '../../api/types.js';
import { selectCronFollowUpChat, selectCronFollowUpExpectation } from '../chat/cronFollowUp.js';

function content(data: unknown): MessageContent {
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
    format: 'json',
    video_url: null,
    filename: null,
    file_url: null,
    refusal: null,
  };
}

function toolOutput(output: unknown, overrides: Partial<TraceMessage> = {}): TraceMessage {
  return {
    id: 'tool-output',
    object: 'message',
    chat_id: 'chat-1',
    sequence: 4,
    type: 'plugin_call_output',
    role: 'tool',
    content: [
      content({
        call_id: 'call-1',
        name: 'CronJob',
        output,
        state: 'success',
      }),
    ],
    status: 'completed',
    metadata: null,
    ...overrides,
  };
}

function chat(sequence: number, userInput: string): ConsoleChat {
  return {
    id: `chat-${sequence}`,
    session_id: 'session-a',
    sequence,
    user_input: userInput,
    datasource_id: 'ds-demo',
    kind: 'simple',
    status: 'running',
    forkable: false,
    last_sequence_number: -1,
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

test('成功 run 当前 Console Session 时生成追更预期', () => {
  const expectation = selectCronFollowUpExpectation(
    [
      toolOutput(
        JSON.stringify({
          ok: true,
          job: {
            id: 'cron-1',
            channel: 'console',
            session_id: 'session-a',
            message: '生成今日简报',
          },
        }),
      ),
    ],
    'session-a',
  );

  assert.deepEqual(expectation, {
    jobId: 'cron-1',
    message: '生成今日简报',
    sessionId: 'session-a',
  });
});

test('create 结果、其他 Session 和失败输出不会触发追更', () => {
  const created = toolOutput(
    JSON.stringify({
      job: {
        id: 'cron-1',
        channel: 'console',
        session_id: 'session-a',
        message: '生成今日简报',
      },
    }),
  );
  const otherSession = toolOutput(
    JSON.stringify({
      ok: true,
      job: {
        id: 'cron-1',
        channel: 'console',
        session_id: 'session-b',
        message: '生成今日简报',
      },
    }),
  );
  const failed = toolOutput('CronJobError: failed', { status: 'failed' });

  assert.equal(selectCronFollowUpExpectation([created], 'session-a'), null);
  assert.equal(selectCronFollowUpExpectation([otherSession], 'session-a'), null);
  assert.equal(selectCronFollowUpExpectation([failed], 'session-a'), null);
});

test('只选择触发轮之后 message 匹配的第一条新 Chat', () => {
  const expectation = {
    jobId: 'cron-1',
    message: '生成今日简报',
    sessionId: 'session-a',
  };
  const selected = selectCronFollowUpChat(
    [chat(5, '生成今日简报'), chat(3, '生成今日简报'), chat(4, '用户排队消息')],
    expectation,
    2,
  );

  assert.equal(selected?.id, 'chat-3');
  assert.equal(selectCronFollowUpChat([chat(2, '生成今日简报')], expectation, 2), null);
});
