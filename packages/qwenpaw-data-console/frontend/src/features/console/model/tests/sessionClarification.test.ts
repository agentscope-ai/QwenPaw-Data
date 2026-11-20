import assert from 'node:assert/strict';
import test from 'node:test';
import { withRunningChatClarification } from '../chat/sessionClarification.js';

test('running chat copies the session clarification flag; finished chats stay', () => {
  const projections = [
    { chat: { status: 'completed', enable_clarification: true } },
    { chat: { status: 'running', enable_clarification: true } },
  ];
  const next = withRunningChatClarification(projections, false);
  assert.equal(next[0].chat.enable_clarification, true);
  assert.equal(next[1].chat.enable_clarification, false);
  assert.equal(projections[1].chat.enable_clarification, true);
});
