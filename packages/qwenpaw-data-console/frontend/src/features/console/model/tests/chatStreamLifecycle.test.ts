import assert from 'node:assert/strict';
import test from 'node:test';
import { selectChatStreamCompletionAction } from '../runtime/chatStreamLifecycle.js';

test('Chat 运行中流被掐掉后应再订', () => {
  assert.equal(selectChatStreamCompletionAction('dropped', 'running'), 'reconnect');
});

test('Chat 运行中干净关流后应再订', () => {
  assert.equal(selectChatStreamCompletionAction('eof', 'running'), 'reconnect');
});

test('Chat 已终态时断开视为正常结束', () => {
  assert.equal(selectChatStreamCompletionAction('dropped', 'completed'), 'terminal');
  assert.equal(selectChatStreamCompletionAction('eof', 'failed'), 'terminal');
});

test('主动取消不重连', () => {
  assert.equal(selectChatStreamCompletionAction('aborted', 'running'), 'aborted');
});

test('非运行态意外断开不重连', () => {
  assert.equal(selectChatStreamCompletionAction('dropped', null), 'unexpected');
});

test('empty stream reconnects back off and cancellation clears pending retries', async (t) => {
  const { waitForStreamReconnect } = await import('../runtime/chatStreamLifecycle.js');
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const controller = new AbortController();
  let resumed = false;
  const first = waitForStreamReconnect(0, controller.signal).then((retry) => {
    resumed = retry;
  });
  t.mock.timers.tick(499);
  await Promise.resolve();
  assert.equal(resumed, false);
  t.mock.timers.tick(1);
  await first;
  assert.equal(resumed, true);
  const later = waitForStreamReconnect(8, controller.signal);
  t.mock.timers.tick(14_999);
  controller.abort();
  assert.equal(await later, false);
  assert.equal(await waitForStreamReconnect(0, controller.signal), false);
});
