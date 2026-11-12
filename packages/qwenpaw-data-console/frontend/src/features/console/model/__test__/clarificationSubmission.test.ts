import assert from 'node:assert/strict';
import test from 'node:test';
import { claimClarificationSubmission } from '../chat/askUserQuestion.js';

test('同一个澄清只允许一个提交路径取得 claim', () => {
  const claimedKeys = new Set<string>();
  const key = 'chat-1:clarification-1';

  assert.equal(claimClarificationSubmission(claimedKeys, key), true);
  assert.equal(claimClarificationSubmission(claimedKeys, key), false);
  assert.equal(claimedKeys.size, 1);
});

test('不同 Chat 或 clarification 仍可独立提交', () => {
  const claimedKeys = new Set<string>();

  assert.equal(claimClarificationSubmission(claimedKeys, 'chat-1:clarification-1'), true);
  assert.equal(claimClarificationSubmission(claimedKeys, 'chat-1:clarification-2'), true);
  assert.equal(claimClarificationSubmission(claimedKeys, 'chat-2:clarification-1'), true);
  assert.equal(claimedKeys.size, 3);
});
