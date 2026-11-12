import { createRuntimeSessionBuilder } from '../runtime/runtimeMessages.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import type {
  ConsoleChat,
  ConsoleSession,
  SessionSnapshot,
  TraceMessage,
} from '../../api/types.js';
import {
  historyCursor,
  mergeHistoryPage,
  hydrateProjectionTrace,
  loadHistoryTraces,
  createHistoryTraceQueue,
} from '../chat/sessionHistory.js';
import { createChatStreamProjection } from '../runtime/projectStream.js';
import {
  mergeHydratedRuntimeMessages,
  shouldHydrateRuntimeHistory,
} from '../runtime/runtimeSessionSync.js';

const turn = (sequence: number) => chat({ id: `chat-${sequence}`, sequence });

test('history uses sequence cursors, sorts pages and preserves live overlapping turns', () => {
  const live = createChatStreamProjection(turn(20));
  const merged = mergeHistoryPage(
    [live, createChatStreamProjection(turn(21))],
    [turn(19), turn(20), turn(17)],
  );
  assert.deepEqual(
    merged.map((p) => p.chat.sequence),
    [17, 19, 20, 21],
  );
  assert.equal(merged[2], live);
  const page = { chats: [turn(19), turn(17)], has_more: true } as SessionSnapshot;
  assert.equal(historyCursor(page), 17);
  assert.equal(historyCursor({ ...page, has_more: false }), null);
  assert.equal(historyCursor({ ...page, chats: [] }), null);
  assert.equal(historyCursor({ chats: page.chats } as SessionSnapshot), null);
});

test('late trace preserves SSE messages, cursor and completed state', () => {
  const message = (id: string, sequence: number, text: string): TraceMessage => ({
    id,
    sequence,
    object: 'message',
    chat_id: 'chat-1',
    role: 'assistant',
    type: 'message',
    content: [
      {
        object: 'content',
        type: 'text',
        text,
        delta: false,
        index: 0,
        status: 'completed',
        msg_id: id,
        image_url: null,
        data: null,
        format: null,
        video_url: null,
        filename: null,
        file_url: null,
        refusal: null,
      },
    ],
    status: 'completed',
    metadata: null,
  });
  const old = message('old', 1, 'old');
  const live = message('live', 2, 'latest SSE');
  const projection = {
    ...createChatStreamProjection(chat({ status: 'completed' }), [live]),
    lastSequenceNumber: 99,
  };
  const hydrated = hydrateProjectionTrace(projection, [old, message('live', 2, 'stale HTTP')]);
  assert.deepEqual(hydrated.messages, [old, live]);
  assert.equal(hydrated.lastSequenceNumber, 99);
  assert.equal(hydrated.chat, projection.chat);
});

test('trace requests are bounded and fast results publish before a slow request completes', async () => {
  const pending = new Map<string, () => void>();
  const controller = new AbortController();
  const published: string[] = [];
  let active = 0,
    maxActive = 0;
  const work = loadHistoryTraces(
    Array.from({ length: 7 }, (_, i) => turn(i)),
    controller.signal,
    async (chat) => {
      active++;
      maxActive = Math.max(maxActive, active);
      await new Promise<void>((resolve) => pending.set(chat.id, resolve));
      active--;
      return [];
    },
    (chat) => published.push(chat.id),
    () => assert.fail('unexpected error'),
  );
  assert.equal(pending.size, 4);
  pending.get('chat-1')!();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(published, ['chat-1']);
  assert.equal(pending.has('chat-4'), true);
  controller.abort();
  for (const resolve of pending.values()) resolve();
  await work;
  assert.deepEqual(published, ['chat-1']);
  assert.equal(maxActive, 4);
  assert.equal(pending.has('chat-5'), false);
});

test('a failed trace does not discard other chats or stop the work queue', async () => {
  const published: string[] = [],
    failed: string[] = [];
  await loadHistoryTraces(
    [turn(1), turn(2)],
    new AbortController().signal,
    async (chat) => {
      if (chat.sequence === 1) throw new Error('network');
      return [];
    },
    (chat) => published.push(chat.id),
    (chat) => failed.push(chat.id),
  );
  assert.deepEqual(published, ['chat-2']);
  assert.deepEqual(failed, ['chat-1']);
});

test('SDK hydration prepends history, refreshes completed cards and retains running/queued input', () => {
  const mounted = [
    { id: 'current', text: 'live' },
    { id: 'done', text: 'empty' },
    { id: 'queued', text: 'unsent' },
  ];
  const snapshot = [
    { id: 'older', text: 'history' },
    { id: 'done', text: 'trace' },
    { id: 'current', text: 'stale' },
  ];
  assert.deepEqual(
    mergeHydratedRuntimeMessages({ mounted, snapshot, protectedIds: new Set(['current']) }),
    [snapshot[0], snapshot[1], mounted[0], mounted[2]],
  );
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

test('first send and SDK remounts must never trigger history synchronization', () => {
  assert.equal(shouldHydrateRuntimeHistory(null, { mountKey: 'new', version: 0 }), false);
  assert.equal(
    shouldHydrateRuntimeHistory(
      { mountKey: 'new', version: 0 },
      { mountKey: 'persisted', version: 0 },
    ),
    false,
  );
  assert.equal(
    shouldHydrateRuntimeHistory(
      { mountKey: 'persisted', version: 0 },
      { mountKey: 'persisted', version: 0 },
    ),
    false,
  );
  assert.equal(
    shouldHydrateRuntimeHistory(
      { mountKey: 'persisted', version: 0 },
      { mountKey: 'persisted', version: 1 },
    ),
    true,
  );
  assert.equal(
    shouldHydrateRuntimeHistory(
      { mountKey: 'previous', version: 7 },
      { mountKey: 'next', version: 8 },
    ),
    false,
  );
});

test('stream updates rebuild only the changed turn and do not mutate historical cards', () => {
  const build = createRuntimeSessionBuilder();
  const session = { id: 'session-1', title: 'History' } as ConsoleSession;
  const first = createChatStreamProjection(chat({ id: 'first', sequence: 1, status: 'completed' }));
  const second = createChatStreamProjection(chat({ id: 'second', sequence: 2 }));
  const before = build(session, [first, second]);
  const changed = { ...second, chat: { ...second.chat, user_input: 'Updated' } };
  const after = build(session, [first, changed]);
  assert.equal(after.messages[0], before.messages[0]);
  assert.equal(after.messages[1], before.messages[1]);
  assert.notEqual(after.messages[2], before.messages[2]);
  assert.notEqual(after.messages[3], before.messages[3]);
  assert.equal(build(session, [first, changed]).messages[3], after.messages[3]);
});

test('empty or irrelevant trace does not trigger a new history version', () => {
  const projection = createChatStreamProjection(chat());
  assert.equal(hydrateProjectionTrace(projection, []), projection);
});

test('visible and selected traces share one bounded queue and successful results are not reloaded', async () => {
  const controller = new AbortController();
  const requests: string[] = [];
  const published: string[] = [];
  const pending = new Map<string, () => void>();
  const tick = () => new Promise<void>((resolve) => setImmediate(resolve));
  const enqueue = createHistoryTraceQueue(
    controller.signal,
    async (chat) => {
      requests.push(chat.id);
      await new Promise<void>((resolve) => pending.set(chat.id, resolve));
      return [];
    },
    (chat) => {
      published.push(chat.id);
    },
    () => assert.fail('unexpected failure'),
  );
  enqueue([turn(1), turn(2), turn(3)]);
  enqueue([turn(1), turn(3), turn(4)]);
  await tick();
  assert.deepEqual(requests, ['chat-1', 'chat-2']);
  pending.get('chat-1')!();
  await tick();
  assert.deepEqual(requests, ['chat-1', 'chat-2', 'chat-3']);
  enqueue([turn(1)]);
  controller.abort();
  pending.get('chat-2')!();
  pending.get('chat-3')!();
  await tick();
  assert.deepEqual(published, ['chat-1']);
  assert.equal(requests.includes('chat-4'), false);
});

test('failed trace can be explicitly retried', async () => {
  const controller = new AbortController();
  let attempts = 0,
    failures = 0,
    published = 0;
  const enqueue = createHistoryTraceQueue(
    controller.signal,
    async () => {
      if (++attempts === 1) throw new Error('offline');
      return [];
    },
    () => {
      published++;
    },
    () => {
      failures++;
    },
  );
  enqueue([turn(1)]);
  await new Promise<void>((resolve) => setImmediate(resolve));
  enqueue([turn(1)]);
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(attempts, 2);
  assert.equal(failures, 1);
  assert.equal(published, 1);
  controller.abort();
});
