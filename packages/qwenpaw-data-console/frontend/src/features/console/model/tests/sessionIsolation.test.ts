import assert from 'node:assert/strict';
import test from 'node:test';
import { selectRuntimeSessionMessages } from '../runtime/runtimeSessionSync.js';

for (const mountedSessionId of ['ses_old', 'new', undefined]) {
  test(`目标会话有队列时也不能复用 ${mountedSessionId} 的消息`, () => {
    const snapshotMessages = [{ id: 'target-question' }];
    const mountedMessages = [{ id: 'old-question' }, { id: 'old-answer' }];
    assert.equal(
      selectRuntimeSessionMessages({
        snapshotMessages,
        mountedMessages,
        snapshotSessionId: 'ses_target',
        mountedSessionId,
        hasQueuedInputs: true,
      }),
      snapshotMessages,
    );
  });
}

test('同会话队列继续使用包含流式回复的 mounted 消息', () => {
  const mountedMessages = [{ id: 'question' }, { id: 'streaming-answer' }];
  assert.equal(
    selectRuntimeSessionMessages({
      snapshotMessages: [{ id: 'question' }],
      mountedMessages,
      snapshotSessionId: 'ses_current',
      mountedSessionId: 'ses_current',
      hasQueuedInputs: true,
    }),
    mountedMessages,
  );
});
