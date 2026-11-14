import assert from 'node:assert/strict';
import { test } from 'node:test';
import { UsageController, usageError, type UsageApi } from '../usage/usageController.js';
import { formatUsageDate, formatUsageNumber, isUsageEmpty } from '../usage/usagePresentation.js';
import type { UsageSummary, UsageWindow } from '../../api/usageTypes.js';
import {
  getUsageSummary,
  getUsageTimeseries,
  getUsageSessions,
  getUsageBackgroundTasks,
} from '../../api/usage.js';

const window: UsageWindow = {
  period: '7d',
  timezone: 'Asia/Shanghai',
  start: '2026-09-14T16:00:00Z',
  end: '2026-09-21T09:00:00Z',
  as_of: '2026-09-21T09:00:00Z',
};
const totals = {
  generation: { input_tokens: 200, output_tokens: 50, total_tokens: 250, calls: 3 },
  embedding: { tokens: 30, calls: 2 },
  total_tokens: 280,
};
const summary: UsageSummary = {
  ...totals,
  window,
  sessions: { count: 23 },
  background_tasks: { count: 2 },
};
const pagination = {
  page: 1,
  page_size: 10,
  total: 23,
  page_count: 3,
  has_previous: false,
  has_next: true,
};
const sessions = { window, items: [], totals: { ...totals, sessions: 23 }, pagination };
const background = {
  window,
  items: [],
  totals: { ...totals, groups: 2 },
  pagination: { ...pagination, total: 2, page_count: 1, has_next: false },
};
const trend = { window, points: [], totals, granularity: 'day' as const };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const flush = () => new Promise<void>((resolve) => setImmediate(resolve));
const api = (overrides: Partial<UsageApi> = {}): UsageApi => ({
  summary: async () => summary,
  trend: async () => trend,
  sessions: async () => sessions,
  background: async () => background,
  ...overrides,
});

test('summary creates the server snapshot before independent regions; slow session does not block other regions', async () => {
  const pendingSummary = deferred<UsageSummary>();
  const pendingSessions = deferred<typeof sessions>();
  const queries: unknown[] = [];
  const store = new UsageController(
    api({
      summary: () => pendingSummary.promise,
      trend: async (query) => {
        queries.push(query);
        return trend;
      },
      sessions: (query) => {
        queries.push(query);
        return pendingSessions.promise;
      },
      background: async (query) => {
        queries.push(query);
        return background;
      },
    }),
  );
  store.setContext(1, true, '7d');
  assert.equal(queries.length, 0);
  pendingSummary.resolve(summary);
  await flush();
  assert.equal(queries.length, 3);
  for (const query of queries) assert.equal((query as { as_of: string }).as_of, window.as_of);
  assert.equal(store.getSnapshot().trend.data, trend);
  assert.equal(store.getSnapshot().background.data, background);
  assert.equal(store.getSnapshot().sessions.loading, true);
  pendingSessions.resolve(sessions);
  await flush();
  assert.equal(store.getSnapshot().sessions.data?.totals.sessions, 23);
});

test('pagination is independent and retains snapshot and full totals', async () => {
  const requests: { page: number; page_size: number; as_of?: string }[] = [];
  const store = new UsageController(
    api({
      sessions: async (query) => {
        requests.push(query);
        return { ...sessions, pagination: { ...pagination, page: query.page } };
      },
    }),
  );
  store.setContext(1, true, '7d');
  await flush();
  const previousBackground = store.getSnapshot().background;
  await store.load('sessions', 2);
  assert.deepEqual(
    requests.map((q) => q.page),
    [1, 2],
  );
  assert.deepEqual(
    requests.map((q) => q.page_size),
    [10, 10],
  );
  assert.equal(requests[1].as_of, window.as_of);
  assert.equal(store.getSnapshot().background, previousBackground);
  assert.equal(store.getSnapshot().sessions.data?.totals.sessions, 23);
});

test('local failure retains other successful regions; retry uses same snapshot and page', async () => {
  let fail = true;
  const store = new UsageController(
    api({
      sessions: async (query) => {
        if (fail) throw { status: 500, message: 'SQL internal detail' };
        return { ...sessions, pagination: { ...pagination, page: query.page } };
      },
    }),
  );
  store.setContext(1, true, '7d');
  await flush();
  assert.equal(store.getSnapshot().sessions.error, 'load');
  assert.equal(store.getSnapshot().trend.data, trend);
  await store.load('sessions', 2);
  fail = false;
  await store.load('sessions');
  assert.equal(store.getSnapshot().sessions.data?.pagination.page, 2);
  assert.equal(store.getSnapshot().sessions.error, null);
});

test('identity change invalidates even an abort-ignoring summary response', async () => {
  const old = deferred<UsageSummary>();
  let count = 0;
  const store = new UsageController(
    api({
      summary: () =>
        ++count === 1 ? old.promise : Promise.resolve({ ...summary, total_tokens: 99 }),
    }),
  );
  store.setContext(1, true, '7d');
  store.setContext(2, false, '7d');
  assert.equal(store.getSnapshot().summary.data, null);
  old.resolve(summary);
  await flush();
  assert.equal(store.getSnapshot().summary.data, null);
  store.setContext(2, true, '7d');
  await flush();
  assert.equal(store.getSnapshot().summary.data?.total_tokens, 99);
});

test('period change resets both pages and rejects old detail response', async () => {
  const old = deferred<typeof sessions>();
  let count = 0;
  const store = new UsageController(
    api({
      summary: async (query) => ({ ...summary, window: { ...window, period: query.period } }),
      sessions: () => (++count === 2 ? old.promise : Promise.resolve(sessions)),
    }),
  );
  store.setContext(1, true, '7d');
  await flush();
  void store.load('sessions', 2);
  await store.load('background', 2);
  store.setContext(1, true, '1d');
  assert.equal(store.getSnapshot().summary.data, null);
  assert.equal(store.getSnapshot().sessionPage, 1);
  assert.equal(store.getSnapshot().backgroundPage, 1);
  await flush();
  old.resolve({ ...sessions, pagination: { ...pagination, page: 2 } });
  await flush();
  assert.equal(store.getSnapshot().sessions.data?.pagination.page, 1);
});

test('late pagination request cannot overwrite a newer page', async () => {
  const slow = deferred<typeof sessions>();
  const store = new UsageController(
    api({
      sessions: (query) =>
        query.page === 2
          ? slow.promise
          : Promise.resolve({ ...sessions, pagination: { ...pagination, page: query.page } }),
    }),
  );
  store.setContext(1, true, '7d');
  await flush();
  void store.load('sessions', 2);
  await store.load('sessions', 1);
  slow.resolve({ ...sessions, pagination: { ...pagination, page: 2 } });
  await flush();
  assert.equal(store.getSnapshot().sessions.data?.pagination.page, 1);
});

test('summary refresh failure retains same-context data and does not mix snapshots', async () => {
  let fail = false;
  const store = new UsageController(
    api({
      summary: async () => {
        if (fail) throw new Error('offline');
        return summary;
      },
    }),
  );
  store.setContext(1, true, '7d');
  await flush();
  fail = true;
  await store.refresh();
  assert.equal(store.getSnapshot().summary.data, summary);
  assert.equal(store.getSnapshot().summary.error, 'load');
  assert.equal(store.getSnapshot().sessions.data, sessions);
});

test('first summary failure never launches dependent requests; stop cancels requests', async () => {
  let calls = 0;
  let signal: AbortSignal | undefined;
  const pending = deferred<UsageSummary>();
  const store = new UsageController(
    api({
      summary: (_query, inputSignal) => {
        signal = inputSignal;
        return pending.promise;
      },
      trend: async () => {
        calls++;
        return trend;
      },
    }),
  );
  store.setContext(1, true, '7d');
  pending.reject({ status: 422 });
  await flush();
  assert.equal(calls, 0);
  assert.equal(store.getSnapshot().summary.error, 'validation');
  store.stop();
  assert.equal(signal?.aborted, true);
  assert.equal(store.getSnapshot().summary.data, null);
});

test('live identity guard prevents writes before React effects update context', async () => {
  let valid = true;
  const pending = deferred<UsageSummary>();
  const store = new UsageController(api({ summary: () => pending.promise }), () => valid);
  store.setContext(1, true, '7d');
  valid = false;
  pending.resolve(summary);
  await flush();
  assert.equal(store.getSnapshot().summary.data, null);
});

test('formatting keeps exact values, timezone and zero-call semantics', () => {
  assert.equal(formatUsageNumber(0, 'en'), '0');
  assert.equal(formatUsageNumber(1234567, 'en'), '1.2M');
  assert.equal(formatUsageNumber(1234567, 'en', false), '1,234,567');
  assert.match(formatUsageDate('2026-09-20T17:30:00Z', 'Asia/Shanghai', 'en'), /21\/09.*01:30/);
  assert.equal(isUsageEmpty({ ...summary, total_tokens: 0 }), false);
  assert.equal(
    isUsageEmpty({
      ...summary,
      generation: { ...totals.generation, calls: 0 },
      embedding: { tokens: 0, calls: 0 },
    }),
    true,
  );
  assert.equal(usageError({ status: 403 }), 'forbidden');
  assert.equal(usageError({ status: 401 }), 'auth');
  assert.equal(usageError({ status: 500, message: 'secret' }), 'load');
});

test('four clients encode snapshot, preserve signal and cookies, and omit identity parameters', async () => {
  const originalFetch = globalThis.fetch;
  const seen: { url: URL; init?: RequestInit }[] = [];
  globalThis.fetch = async (input, init) => {
    seen.push({ url: new URL(String(input), 'http://localhost'), init });
    return new Response('{}', { headers: { 'content-type': 'application/json' } });
  };
  try {
    const request = new AbortController();
    const query = {
      period: '7d' as const,
      timezone: 'Asia/Shanghai',
      as_of: window.as_of,
      page: 2,
      page_size: 10,
      user_id: 'must-not-send',
    };
    await getUsageSummary(query, request.signal);
    await getUsageTimeseries(query, request.signal);
    await getUsageSessions(query, request.signal);
    await getUsageBackgroundTasks(query, request.signal);
    assert.deepEqual(
      seen.map(({ url }) => url.pathname),
      [
        '/api/v1/usage/summary',
        '/api/v1/usage/timeseries',
        '/api/v1/usage/sessions',
        '/api/v1/usage/background-tasks',
      ],
    );
    for (const { url, init } of seen) {
      assert.equal(url.searchParams.get('as_of'), window.as_of);
      assert.equal(url.searchParams.get('timezone'), 'Asia/Shanghai');
      assert.equal(url.searchParams.has('user_id'), false);
      assert.equal(init?.signal, request.signal);
      assert.equal(init?.credentials, 'include');
    }
    assert.equal(seen[0].url.searchParams.has('page'), false);
    assert.equal(seen[2].url.searchParams.get('page'), '2');
    assert.equal(seen[2].url.searchParams.get('page_size'), '10');
    assert.equal(seen[3].url.searchParams.get('page_size'), '10');
  } finally {
    globalThis.fetch = originalFetch;
  }
});
