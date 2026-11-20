import assert from 'node:assert/strict';
import test from 'node:test';
import {
  cacheRuntimeInputDraft,
  clearAcceptedRuntimeInputDraft,
  clearRuntimeInputDraft,
  getRuntimeInputAttachmentNames,
  getRuntimeInputDraft,
  moveRuntimeInputCache,
  rememberRuntimeInputAttachmentName,
  resetRuntimeInputCacheForTest,
} from '../chat/runtimeInputCache.js';

test.beforeEach(resetRuntimeInputCacheForTest);

test('按 Session 隔离未发送草稿', () => {
  cacheRuntimeInputDraft('session-a', 'A 的未发送内容');
  cacheRuntimeInputDraft('session-b', 'B 的未发送内容');

  assert.equal(getRuntimeInputDraft('session-a'), 'A 的未发送内容');
  assert.equal(getRuntimeInputDraft('session-b'), 'B 的未发送内容');
  clearRuntimeInputDraft('session-a');
  assert.equal(getRuntimeInputDraft('session-a'), '');
  assert.equal(getRuntimeInputDraft('session-b'), 'B 的未发送内容');
});

test('新会话持久化后迁移草稿和已添加附件名', () => {
  cacheRuntimeInputDraft('draft-session', '继续分析');
  rememberRuntimeInputAttachmentName('draft-session', 'query.sql');

  moveRuntimeInputCache('draft-session', 'session-persisted');

  assert.equal(getRuntimeInputDraft('draft-session'), '');
  assert.equal(getRuntimeInputDraft('session-persisted'), '继续分析');
  assert.deepEqual(getRuntimeInputAttachmentNames('session-persisted'), ['query.sql']);
});

test('附件名按 Session 隔离并自动去重', () => {
  rememberRuntimeInputAttachmentName('session-a', 'report.html');
  rememberRuntimeInputAttachmentName('session-a', 'report.html');

  assert.deepEqual(getRuntimeInputAttachmentNames('session-a'), ['report.html']);
  assert.deepEqual(getRuntimeInputAttachmentNames('session-b'), []);
});

test('首条消息被接收后清除迁移的草稿，回复结束不再恢复已发送内容', () => {
  cacheRuntimeInputDraft('draft-session', ' hi\n');
  rememberRuntimeInputAttachmentName('draft-session', 'query.sql');
  moveRuntimeInputCache('draft-session', 'session-persisted');

  clearAcceptedRuntimeInputDraft('session-persisted', 'hi');

  assert.equal(getRuntimeInputDraft('session-persisted'), '');
  assert.equal(getRuntimeInputDraft('draft-session'), '');
  assert.deepEqual(getRuntimeInputAttachmentNames('session-persisted'), ['query.sql']);
});

test('消息被接收时保留等待期间新写的草稿及其他会话草稿', () => {
  cacheRuntimeInputDraft('session-a', 'hi');
  cacheRuntimeInputDraft('session-a', '下一条问题');
  cacheRuntimeInputDraft('session-b', 'hi');

  clearAcceptedRuntimeInputDraft('session-a', 'hi');

  assert.equal(getRuntimeInputDraft('session-a'), '下一条问题');
  assert.equal(getRuntimeInputDraft('session-b'), 'hi');
});

test('仅附件消息被接收时不清除文本草稿', () => {
  cacheRuntimeInputDraft('session-a', '未发送的问题');

  clearAcceptedRuntimeInputDraft('session-a', '');

  assert.equal(getRuntimeInputDraft('session-a'), '未发送的问题');
});
