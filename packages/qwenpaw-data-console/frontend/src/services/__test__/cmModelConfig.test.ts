import assert from 'node:assert/strict';
import test from 'node:test';
import { createCMModelConfigGate, fetchCMModelConfigStatus } from '../cmModelConfig.js';
import type { CMModelConfigStatus } from '../cmModelConfig.js';

test('未配置时拒绝发送，配置完成后重查可恢复，队列发送前重新检测', async () => {
  let configured = false;
  const gate = createCMModelConfigGate(async () => ({ determined: true, configured }));
  assert.equal(await gate.check(), false);
  assert.equal(gate.getSnapshot(), true);
  configured = true;
  assert.equal(await gate.check(), true);
  assert.equal(gate.getSnapshot(), false);
  // Configuration can be removed after a message was admitted to the queue.
  configured = false;
  assert.equal(await gate.check(), false);
  assert.equal(gate.getSnapshot(), true);
});

test('并发预检共享请求，不让后台探测与发送探测互相覆盖', async () => {
  let calls = 0;
  let resolve!: (status: CMModelConfigStatus) => void;
  const gate = createCMModelConfigGate(() => {
    calls += 1;
    return new Promise<CMModelConfigStatus>((done) => {
      resolve = done;
    });
  });
  const first = gate.check();
  const second = gate.check();
  assert.equal(calls, 1);
  resolve({ determined: true, configured: false });
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
});

test('会话订阅卸载和重挂载保留提示，配置好后通知当前订阅者', async () => {
  let configured = false;
  const gate = createCMModelConfigGate(async () => ({ determined: true, configured }));
  let oldNotifications = 0;
  const unsubscribe = gate.subscribe(() => {
    oldNotifications += 1;
  });
  await gate.check();
  unsubscribe();
  assert.equal(gate.getSnapshot(), true);
  let newNotifications = 0;
  gate.subscribe(() => {
    newNotifications += 1;
  });
  assert.equal(gate.getSnapshot(), true);
  configured = true;
  await gate.check();
  assert.equal(oldNotifications, 1);
  assert.equal(newNotifications, 1);
  assert.equal(gate.getSnapshot(), false);
});

test('旧身份的慢响应不能覆盖新身份的状态', async () => {
  let resolve!: (status: CMModelConfigStatus) => void;
  const oldGate = createCMModelConfigGate(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const oldRequest = oldGate.check();
  const newGate = createCMModelConfigGate(async () => ({ determined: true, configured: true }));
  await newGate.check();
  resolve({ determined: true, configured: false });
  await oldRequest;
  assert.equal(newGate.getSnapshot(), false);
});

test('探测不确定时放行且不伪装成已配置，后续重试仍生效', async () => {
  let status: CMModelConfigStatus = { determined: true, configured: false };
  const gate = createCMModelConfigGate(async () => status);
  await gate.check();
  status = { determined: false, configured: false };
  assert.equal(await gate.check(), true);
  assert.equal(gate.getSnapshot(), true);
  status = { determined: true, configured: true };
  assert.equal(await gate.check(), true);
  assert.equal(gate.getSnapshot(), false);
});

test('实际探测携带 Cookie；HTTP 或网络失败按不确定处理', async (context) => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { setTimeout, clearTimeout },
  });
  context.after(() => {
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  });
  let response = Response.json({ configured: false });
  const mock = context.mock.method(
    globalThis,
    'fetch',
    async (url: RequestInfo | URL, options?: RequestInit) => {
      assert.equal(url, '/api/system/model-config/');
      assert.equal(options?.credentials, 'include');
      assert.ok(options?.signal instanceof AbortSignal);
      return response;
    },
  );
  assert.deepEqual(await fetchCMModelConfigStatus(), { determined: true, configured: false });
  response = Response.json({ configured: true });
  assert.deepEqual(await fetchCMModelConfigStatus(), { determined: true, configured: true });
  response = new Response(null, { status: 401 });
  assert.deepEqual(await fetchCMModelConfigStatus(), { determined: false, configured: false });
  mock.mock.mockImplementation(async () => {
    throw new TypeError('network');
  });
  assert.deepEqual(await fetchCMModelConfigStatus(), { determined: false, configured: false });
});
