import assert from 'node:assert/strict';
import test from 'node:test';
import type { Artifact, SessionSegmentArtifact } from '../../api/types.js';
import {
  createSessionArtifactsController,
  parseSessionArtifacts,
  toSessionBusinessArtifact,
  filterSessionArtifacts,
  type ArtifactScope,
} from '../artifacts/sessionArtifacts.js';

const scope: ArtifactScope = { sessionId: 'A', revision: 1, epoch: 1 };
const artifact = (path: string, time = 1, extra: Partial<Artifact> = {}): Artifact => ({
  id: `${path}-${time}`,
  session_id: 'A',
  chat_id: 'chat-1',
  name: path,
  path,
  created_at: '2026-10-28T00:00:00Z',
  updated_at: `2026-10-28T00:00:0${time}Z`,
  ...extra,
});
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));
const business = (path = 'report.html'): SessionSegmentArtifact => ({
  name: 'First report.html',
  description: 'First description',
  relative_path: path,
  chat_id: 'old-chat',
  segments: [
    { chat_id: 'old-chat', segment_id: 'seg-1', title: 'First analysis' },
    { chat_id: 'new-chat', segment_id: 'seg-1', title: 'Second analysis' },
  ],
});
function harness() {
  const requests: {
    sessionId: string;
    signal: AbortSignal;
    resolve: (value: unknown) => void;
    reject: (error: unknown) => void;
  }[] = [];
  let notifications = 0;
  const controller = createSessionArtifactsController(
    (sessionId, signal) =>
      new Promise((resolve, reject) => requests.push({ sessionId, signal, resolve, reject })),
    () => {
      notifications++;
    },
  );
  return { controller, requests, notifications: () => notifications };
}

test('business response is required and validated independently from the Agent list', () => {
  for (const segment_artifacts of [
    undefined,
    null,
    ['error'],
    [{ ...business(), segments: [] }],
    [{ ...business(), relative_path: '' }],
    [{ ...business(), segments: [{ segment_id: 'x' }] }],
  ]) {
    assert.throws(() => parseSessionArtifacts({ artifacts: [], segment_artifacts }, 'A'));
  }
  assert.deepEqual(
    parseSessionArtifacts({ artifacts: [], segment_artifacts: [business()] }, 'A')
      .segment_artifacts,
    [business()],
  );
});

test('local search matches names, paths and descriptions without changing source order', () => {
  const items = [
    toSessionBusinessArtifact(business('reports/sales.html')),
    { id: 'agent', name: 'DEBUG.JSON', title: '', type: 'JSON', meta: '', path: 'tmp/debug.json' },
  ];
  assert.equal(filterSessionArtifacts(items, '  '), items);
  assert.deepEqual(filterSessionArtifacts(items, ' SALES '), [items[0]]);
  assert.deepEqual(filterSessionArtifacts(items, 'description'), [items[0]]);
  assert.deepEqual(filterSessionArtifacts(items, 'debug.json'), [items[1]]);
  assert.deepEqual(filterSessionArtifacts(items, 'missing'), []);
  assert.equal(items.length, 2);
});

test('search covers the full list before slicing pages, including late and updated files', () => {
  const items = Array.from({ length: 60 }, (_, i) =>
    toSessionBusinessArtifact({
      ...business(`batch-${i + 1}/data.csv`),
      name: `data-${i + 1}.csv`,
      description: '',
    }),
  );
  assert.equal(filterSessionArtifacts(items, 'batch-60')[0].path, 'batch-60/data.csv');
  assert.equal(filterSessionArtifacts(items, 'data').slice(20, 40).length, 20);
  assert.equal(filterSessionArtifacts(items, 'data').length, 60);
  const updated = [...items, toSessionBusinessArtifact(business('batch-60/report.html'))];
  assert.equal(filterSessionArtifacts(updated, 'batch-60').length, 2);
});

test('business mapping preserves first metadata and cross-chat references without invented numbers', () => {
  const result = toSessionBusinessArtifact(business());
  assert.equal(result.title, 'First description');
  assert.equal(result.name, 'First report.html');
  assert.equal(result.access?.relativePath, 'report.html');
  assert.equal(new Set(result.segments?.map((s) => s.id)).size, 2);
  assert.ok(result.segments?.every((s) => s.number === undefined));
});

test('business snapshots replace removed paths and references while Agent updates stay merged', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].resolve({
    artifacts: [artifact('intermediate.csv')],
    segment_artifacts: [business(), business('old.md')],
  });
  await tick();
  assert.equal(controller.getSnapshot().segmentArtifacts.length, 2);
  controller.segmentChanged();
  await tick();
  const revised = { ...business(), segments: business().segments.slice(0, 1) };
  requests[1].resolve({ artifacts: [], segment_artifacts: [revised] });
  await tick();
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, [revised]);
  assert.equal(controller.getSnapshot().artifacts.length, 1);
  controller.segmentChanged();
  await tick();
  requests[2].resolve({ artifacts: [], segment_artifacts: [] });
  await tick();
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, []);
  assert.equal(controller.getSnapshot().artifacts.length, 1);
});

test('segment notifications during loading queue one authoritative trailing snapshot', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  controller.segmentChanged();
  controller.segmentChanged();
  requests[0].resolve({ artifacts: [], segment_artifacts: [] });
  await tick();
  assert.equal(requests.length, 2);
  requests[1].resolve({ artifacts: [], segment_artifacts: [business()] });
  await tick();
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, [business()]);
  assert.equal(requests.length, 2);
});

test('business failures preserve data, access loss clears both lists and blocks automatic sync', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].resolve({ artifacts: [artifact('x')], segment_artifacts: [business()] });
  await tick();
  controller.segmentChanged();
  await tick();
  requests[1].resolve({ artifacts: [] });
  await tick();
  assert.equal(controller.getSnapshot().error, true);
  assert.equal(controller.getSnapshot().segmentArtifacts.length, 1);
  assert.equal(controller.getSnapshot().artifacts.length, 1);
  controller.segmentChanged();
  await tick();
  requests[2].reject({ status: 403 });
  await tick();
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, []);
  assert.deepEqual(controller.getSnapshot().artifacts, []);
  controller.segmentChanged();
  await tick();
  assert.equal(requests.length, 3);
  const retry = controller.refresh();
  await tick();
  requests[3].resolve({ artifacts: [], segment_artifacts: [business()] });
  await retry;
  assert.equal(controller.getSnapshot().segmentArtifacts.length, 1);
});

test('identity change clears business data, cancels queued sync and rejects late business snapshots', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].resolve({ artifacts: [], segment_artifacts: [business()] });
  await tick();
  controller.segmentChanged();
  await tick();
  controller.segmentChanged();
  controller.ensureScope({ ...scope, revision: 2 });
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, []);
  await tick();
  requests[1].resolve({ artifacts: [], segment_artifacts: [business('private')] });
  requests[2].resolve({ artifacts: [], segment_artifacts: [] });
  await tick();
  assert.equal(requests.length, 3);
  assert.deepEqual(controller.getSnapshot().segmentArtifacts, []);
});

test('valid empty responses differ from malformed or foreign-session responses', () => {
  assert.deepEqual(parseSessionArtifacts({ segment_artifacts: [], artifacts: [] }, 'A'), {
    segment_artifacts: [],
    artifacts: [],
  });
  for (const value of [
    null,
    [],
    ['error'],
    {},
    { segment_artifacts: [], artifacts: null },
    { segment_artifacts: [], artifacts: ['error'] },
    { segment_artifacts: [], artifacts: [artifact('x', 1, { session_id: 'B' })] },
    { segment_artifacts: [], artifacts: [artifact('x', 1, { updated_at: 'bad date' })] },
    { segment_artifacts: [], artifacts: [{ path: 'x' }] },
  ]) {
    assert.throws(() => parseSessionArtifacts(value, 'A'));
  }
});

test('drafts do not request; creation initializes synchronously and repeated effects keep early events', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(null);
  await controller.refresh();
  assert.equal(requests.length, 0);
  controller.ensureScope(scope);
  controller.register(artifact('first.csv', 2));
  controller.ensureScope({ ...scope });
  await tick();
  assert.equal(requests.length, 1);
  requests[0].resolve({ segment_artifacts: [], artifacts: [] });
  await tick();
  assert.equal(controller.getSnapshot().artifacts[0].path, 'first.csv');
  assert.equal(controller.getSnapshot().hasLoaded, true);
});

test('full list includes unloaded turns and merges by path, timestamp and stable tie order', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].resolve({
    segment_artifacts: [],
    artifacts: [artifact('old.csv'), artifact('report.html', 2)],
  });
  await tick();
  controller.register(artifact('report.html', 4, { chat_id: 'unloaded-chat', id: 'new-id' }));
  controller.register(artifact('other.csv', 3));
  controller.register(artifact('report.html', 2));
  controller.register(artifact('other.csv', 3));
  // Same timestamp, different timezone representation; compare instants, not strings.
  controller.register(artifact('tie.csv', 3, { updated_at: '2026-10-28T08:00:03+08:00' }));
  assert.deepEqual(
    controller.getSnapshot().artifacts.map((a) => a.path),
    ['report.html', 'other.csv', 'tie.csv', 'old.csv'],
  );
  assert.equal(controller.getSnapshot().artifacts[0].id, 'new-id');
});

test('late initial and refresh responses cannot roll back newer or same-time live events', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  controller.register(artifact('x', 2));
  controller.register(artifact('tie', 1, { id: 'live' }));
  requests[0].resolve({
    segment_artifacts: [],
    artifacts: [artifact('x', 1), artifact('tie', 1, { id: 'http' })],
  });
  await tick();
  assert.equal(controller.getSnapshot().artifacts[0].updated_at, artifact('x', 2).updated_at);
  assert.equal(controller.getSnapshot().artifacts[1].id, 'live');
  const refresh = controller.refresh();
  await tick();
  controller.register(artifact('x', 4));
  requests[1].resolve({ segment_artifacts: [], artifacts: [artifact('x', 3), artifact('new', 5)] });
  await refresh;
  assert.deepEqual(
    controller.getSnapshot().artifacts.map((a) => a.id),
    ['new-5', 'x-4', 'live'],
  );
});

test('A to B to A rejects late successes and failures even when the session id matches again', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  controller.register(artifact('old-A'));
  controller.ensureScope({ ...scope, sessionId: 'B', epoch: 2 });
  assert.equal(controller.getSnapshot().artifacts.length, 0);
  await tick();
  controller.ensureScope({ ...scope, epoch: 3 });
  await tick();
  assert.ok(requests[0].signal.aborted);
  assert.ok(requests[1].signal.aborted);
  requests[0].resolve({ segment_artifacts: [], artifacts: [artifact('stale-A')] });
  requests[1].reject({ status: 404 });
  requests[2].resolve({ segment_artifacts: [], artifacts: [artifact('current-A')] });
  await tick();
  assert.deepEqual(
    controller.getSnapshot().artifacts.map((a) => a.path),
    ['current-A'],
  );
  assert.equal(controller.getSnapshot().error, false);
});

test('identity changes in the same session clear state and invalidate pending work', async () => {
  const { controller, requests, notifications } = harness();
  controller.ensureScope(scope);
  await tick();
  controller.register(artifact('old-user'));
  controller.ensureScope({ ...scope, revision: 2 });
  await tick();
  requests[0].reject(new Error('old failure'));
  requests[1].resolve({ segment_artifacts: [], artifacts: [] });
  await tick();
  assert.deepEqual(controller.getSnapshot().artifacts, []);
  assert.equal(controller.getSnapshot().error, false);
  const refresh = controller.refresh(true);
  await tick();
  controller.dispose();
  const count = notifications();
  requests[2].resolve({ segment_artifacts: [], artifacts: [artifact('late')] });
  await refresh;
  assert.equal(notifications(), count);
  assert.equal(controller.getSnapshot().scope, null);
});

test('initial failure retains partial events and a retry supplies the complete list', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].reject(new Error('offline'));
  await tick();
  controller.register(artifact('live'));
  assert.equal(controller.getSnapshot().hasLoaded, false);
  assert.equal(controller.getSnapshot().error, true);
  assert.equal(controller.getSnapshot().artifacts.length, 1);
  const retry = controller.refresh();
  await tick();
  requests[1].resolve({ segment_artifacts: [], artifacts: [artifact('history')] });
  await retry;
  assert.equal(controller.getSnapshot().hasLoaded, true);
  assert.equal(controller.getSnapshot().error, false);
  assert.equal(controller.getSnapshot().artifacts.length, 2);
});

test('malformed successes become failures; refresh failure preserves the valid list', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  await tick();
  requests[0].resolve({ segment_artifacts: [], artifacts: 'not a list' });
  await tick();
  assert.equal(controller.getSnapshot().error, true);
  assert.equal(controller.getSnapshot().hasLoaded, false);
  const retry = controller.refresh();
  await tick();
  requests[1].resolve({ segment_artifacts: [], artifacts: [artifact('valid')] });
  await retry;
  const refresh = controller.refresh();
  assert.equal(controller.getSnapshot().artifacts.length, 1);
  await tick();
  requests[2].reject(new Error('offline'));
  await refresh;
  assert.equal(controller.getSnapshot().artifacts[0].path, 'valid');
  assert.equal(controller.getSnapshot().hasLoaded, true);
  assert.equal(controller.getSnapshot().error, true);
});

for (const status of [401, 403, 404]) {
  test(`access loss ${status} clears the list and rejects events until successful retry`, async () => {
    const { controller, requests } = harness();
    controller.ensureScope(scope);
    controller.register(artifact('private'));
    await tick();
    requests[0].reject({ status });
    await tick();
    controller.register(artifact('late-notification'));
    assert.equal(controller.getSnapshot().unavailable, true);
    assert.deepEqual(controller.getSnapshot().artifacts, []);
    const retry = controller.refresh();
    await tick();
    controller.register(artifact('during-retry'));
    requests[1].resolve({ segment_artifacts: [], artifacts: [] });
    await retry;
    assert.equal(controller.getSnapshot().unavailable, false);
    assert.equal(controller.getSnapshot().hasLoaded, true);
    assert.deepEqual(controller.getSnapshot().artifacts, []);
    controller.register(artifact('after-recovery'));
    assert.equal(controller.getSnapshot().artifacts.length, 1);
  });
}

test('concurrent refreshes coalesce and terminal sync queues exactly one trailing request', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  const initial = controller.refresh();
  assert.equal(controller.refresh(), initial);
  controller.refresh(true);
  controller.refresh(true);
  await tick();
  assert.equal(requests.length, 1);
  requests[0].resolve({ segment_artifacts: [], artifacts: [] });
  await tick();
  assert.equal(requests.length, 2);
  requests[1].resolve({ segment_artifacts: [], artifacts: [artifact('terminal-file')] });
  await tick();
  assert.equal(requests.length, 2);
  assert.equal(controller.getSnapshot().artifacts[0].path, 'terminal-file');
});

test('navigation drops a queued trailing refresh and ignores foreign-session events', async () => {
  const { controller, requests } = harness();
  controller.ensureScope(scope);
  controller.refresh(true);
  await tick();
  controller.ensureScope({ ...scope, sessionId: 'B', epoch: 2 });
  controller.register(artifact('wrong-session'));
  await tick();
  requests[0].resolve({ segment_artifacts: [], artifacts: [artifact('stale')] });
  requests[1].resolve({ segment_artifacts: [], artifacts: [] });
  await tick();
  assert.equal(requests.length, 2);
  assert.deepEqual(controller.getSnapshot().artifacts, []);
});
