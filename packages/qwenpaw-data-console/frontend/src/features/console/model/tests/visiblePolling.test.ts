import assert from 'node:assert/strict';
import test from 'node:test';
import { startVisiblePolling } from '../polling/visiblePolling.js';

class Visibility extends EventTarget {
  visibilityState = 'visible';
  change(state: string) {
    this.visibilityState = state;
    this.dispatchEvent(new Event('visibilitychange'));
  }
}

test('slow polls cannot overlap timers or visibility refreshes; next interval starts after completion', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  let calls = 0;
  let finish!: () => void;
  const stop = startVisiblePolling(
    () => {
      calls++;
      return new Promise<void>((resolve) => {
        finish = resolve;
      });
    },
    1000,
    visibility,
  );
  t.after(stop);
  assert.equal(calls, 1);
  t.mock.timers.tick(10_000);
  visibility.change('hidden');
  visibility.change('visible');
  assert.equal(calls, 1);
  finish();
  await Promise.resolve();
  t.mock.timers.tick(999);
  assert.equal(calls, 1);
  t.mock.timers.tick(1);
  assert.equal(calls, 2);
  stop();
  finish();
  await Promise.resolve();
  t.mock.timers.tick(10_000);
  visibility.change('visible');
  assert.equal(calls, 2);
});

test('background polling pauses, resumes once visible, and retries rejected requests', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  visibility.change('hidden');
  let calls = 0;
  const stop = startVisiblePolling(
    async () => {
      calls++;
      throw new Error('offline');
    },
    1000,
    visibility,
  );
  t.after(stop);
  t.mock.timers.tick(10_000);
  assert.equal(calls, 0);
  visibility.change('visible');
  await Promise.resolve();
  assert.equal(calls, 1);
  visibility.change('hidden');
  t.mock.timers.tick(10_000);
  assert.equal(calls, 1);
  visibility.change('visible');
  await Promise.resolve();
  t.mock.timers.tick(1000);
  assert.equal(calls, 3);
});

test('manual and scheduled refreshes share only pending requests, including effect remounts', async () => {
  const { sharePendingRequest } = await import('../polling/visiblePolling.js');
  const pending = new Map<string, Promise<number>>();
  let calls = 0;
  let finish!: (value: number) => void;
  const request = () => {
    calls++;
    return new Promise<number>((resolve) => {
      finish = resolve;
    });
  };
  const first = sharePendingRequest(pending, 'session', request);
  const second = sharePendingRequest(pending, 'session', request);
  assert.equal(first, second);
  assert.equal(calls, 1);
  finish(42);
  assert.deepEqual(await Promise.all([first, second]), [42, 42]);
  const next = sharePendingRequest(pending, 'session', request);
  assert.equal(calls, 2);
  finish(43);
  assert.equal(await next, 43);
  await assert.rejects(
    sharePendingRequest(pending, 'session', async () => {
      throw new Error('offline');
    }),
  );
  assert.equal(pending.size, 0);
});
