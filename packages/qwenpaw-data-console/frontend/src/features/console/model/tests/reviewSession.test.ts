import assert from 'node:assert/strict';
import test from 'node:test';
import type { Artifact, ConsoleChat, ConsoleSession } from '../../api/types.js';
import {
  appendReviewPrompt,
  restoredAttachmentFiles,
  restoredAttachmentIds,
  reviewReportFilename,
  selectReviewReport,
} from '../chat/reviewSession.js';
import { parseAttachmentReferenceUrl } from '../chat/attachments.js';
import {
  cacheRuntimeInputAttachments,
  cacheRuntimeInputDraft,
  consumeRuntimeInputAttachments,
  getRuntimeInputAttachments,
  getRuntimeInputDraft,
  moveRuntimeInputCache,
  resetRuntimeInputCacheForTest,
  subscribeRuntimeInputAttachments,
} from '../chat/runtimeInputCache.js';

const session: ConsoleSession = {
  id: 'review',
  agent_id: 'review',
  parent_session_id: 'main',
  title: 'Review',
  status: 'idle',
  datasource_id: null,
  chat_count: 1,
  channel: 'console',
  forked_from_chat_id: null,
  created_at: '2026-09-11',
  updated_at: '2026-09-11',
  enable_clarification: true,
};
const report: Artifact = {
  id: 'report',
  session_id: 'review',
  chat_id: 'chat',
  name: 'review-report.md',
  path: 'artifacts/review-report.md',
  created_at: '2026-09-11',
  updated_at: '2026-09-11',
};
const chat = (patch: Partial<ConsoleChat> = {}): ConsoleChat => ({
  id: 'chat',
  session_id: 'review',
  sequence: 1,
  user_input: 'Review',
  datasource_id: null,
  kind: 'simple',
  status: 'completed',
  forkable: false,
  last_sequence_number: 0,
  started_at: null,
  completed_at: null,
  active_duration_ms: 0,
  error: null,
  plan: null,
  segments: [],
  biz_events: [],
  artifacts: [report],
  followup: null,
  ...patch,
});

test.beforeEach(resetRuntimeInputCacheForTest);

test('restored files provide the MIME type required by SDK 1.1 and preserve parent attachment IDs', () => {
  const attachment = { attachment_id: 'parent-upload', filename: 'review-report.md' };
  const [file] = restoredAttachmentFiles([attachment]);
  assert.equal(file.type, 'application/octet-stream');
  assert.equal(file.status, 'done');
  assert.equal(file.file_id, attachment.attachment_id);
  assert.deepEqual(parseAttachmentReferenceUrl(file.response.url), attachment);
  assert.deepEqual(restoredAttachmentIds('["parent-upload",null,1]'), ['parent-upload']);
  assert.deepEqual(restoredAttachmentIds('bad-json'), []);
});

test('only a linked review session with a completed final report can return it', () => {
  assert.equal(selectReviewReport(session, [chat()]), report);
  for (const current of [
    null,
    { ...session, agent_id: 'default' },
    { ...session, parent_session_id: null },
    { ...session, status: 'running' as const },
  ]) {
    assert.equal(selectReviewReport(current, [chat()]), null);
  }
  for (const status of ['created', 'running', 'failed', 'canceled'] as const) {
    assert.equal(selectReviewReport(session, [chat({ status })]), null);
  }
  assert.equal(
    selectReviewReport(session, [chat(), chat({ id: 'next', status: 'running', artifacts: [] })]),
    null,
  );
});

test('walkthrough files, wrong sessions and arbitrary Markdown are not final reports', () => {
  for (const path of [
    'artifacts/requirement-map.md',
    'artifacts/walkthrough.md',
    'review-report.md.bak',
  ]) {
    assert.equal(selectReviewReport(session, [chat({ artifacts: [{ ...report, path }] })]), null);
  }
  assert.equal(
    selectReviewReport(session, [chat({ artifacts: [{ ...report, session_id: 'main' }] })]),
    null,
  );
});

test('selects the latest completed report independently of selected historical chat', () => {
  const latest = { ...report, id: 'new', updated_at: '2026-09-12' };
  assert.equal(selectReviewReport(session, [chat({ artifacts: [latest] }), chat()]), latest);
});

test('returning a report preserves the parent draft and unrelated session attachments', () => {
  const original = { attachment_id: 'original', filename: 'data.csv' };
  const returned = { attachment_id: 'returned', filename: 'review-report-unique.md' };
  cacheRuntimeInputDraft('main', 'Do not lose this');
  cacheRuntimeInputAttachments('main', [original]);
  cacheRuntimeInputAttachments('review', [returned]);
  cacheRuntimeInputDraft('main', appendReviewPrompt(getRuntimeInputDraft('main'), 'Please revise'));
  cacheRuntimeInputAttachments('main', [returned, returned]);
  assert.equal(getRuntimeInputDraft('main'), 'Do not lose this\n\nPlease revise');
  assert.deepEqual(getRuntimeInputAttachments('main'), [original, returned]);
  consumeRuntimeInputAttachments('main', ['returned']);
  assert.deepEqual(getRuntimeInputAttachments('main'), [original]);
  assert.deepEqual(getRuntimeInputAttachments('review'), [returned]);
});

test('consumption only removes captured IDs and leaves queued snapshots immutable', () => {
  const first = { attachment_id: 'first', filename: 'first.md' };
  const second = { attachment_id: 'second', filename: 'second.md' };
  cacheRuntimeInputAttachments('main', [first]);
  const submitted = getRuntimeInputAttachments('main');
  cacheRuntimeInputAttachments('main', [second]);
  consumeRuntimeInputAttachments(
    'main',
    submitted.map((item) => item.attachment_id),
  );
  assert.deepEqual(submitted, [first]);
  assert.deepEqual(getRuntimeInputAttachments('main'), [second]);
  cacheRuntimeInputAttachments('main', submitted); // Rejected submission restores the captured files.
  assert.deepEqual(getRuntimeInputAttachments('main'), [second, first]);
});

test('draft migration preserves attachment references and notifies subscribers', () => {
  let notifications = 0;
  const unsubscribe = subscribeRuntimeInputAttachments(() => {
    notifications += 1;
  });
  const file = { attachment_id: 'one', filename: 'report.md' };
  cacheRuntimeInputAttachments('draft', [file]);
  moveRuntimeInputCache('draft', 'main');
  assert.deepEqual(getRuntimeInputAttachments('draft'), []);
  assert.deepEqual(getRuntimeInputAttachments('main'), [file]);
  consumeRuntimeInputAttachments('main', ['one']);
  unsubscribe();
  assert.equal(notifications, 3);
});

test('report filenames retain the Markdown extension and differ for repeated returns', () => {
  assert.equal(reviewReportFilename('first'), 'review-report-first.md');
  assert.notEqual(reviewReportFilename('first'), reviewReportFilename('second'));
  assert.equal(appendReviewPrompt('', 'Review'), 'Review');
});
