import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { GT_SCROLL_CHANNEL, injectGtScrollBridge } from '../artifacts/artifactPreview.js';

test('HTML bridge scrolls a nested report before its first user scroll and ignores other senders', () => {
  const handlers = new Map<string, (event: any) => void>();
  const messages: any[] = [];
  const root = { scrollHeight: 500, clientHeight: 500, scrollTop: 0 };
  const main = {
    isConnected: true,
    scrollHeight: 2000,
    clientHeight: 500,
    clientWidth: 800,
    scrollTop: 0,
  };
  const parent = { postMessage: (message: any) => messages.push(message) };
  const document = {
    scrollingElement: root,
    documentElement: root,
    body: root,
    readyState: 'complete',
    querySelectorAll: (selector: string) => (selector === 'body *' ? [main] : []),
    addEventListener: () => {},
  };
  const window = {
    parent,
    getComputedStyle: () => ({ overflowY: 'auto' }),
    addEventListener: (name: string, handler: (event: any) => void) => handlers.set(name, handler),
    requestAnimationFrame: (callback: () => void) => callback(),
    setTimeout: (callback: () => void) => callback(),
  };
  const html = injectGtScrollBridge('<html><body><main>Report</main></body></html>');
  const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script);
  vm.runInNewContext(script, { window, document });
  const message = handlers.get('message');
  assert.ok(message);
  message({ source: {}, data: { channel: GT_SCROLL_CHANNEL, kind: 'apply', top: 300 } });
  assert.equal(main.scrollTop, 0);
  message({ source: parent, data: { channel: GT_SCROLL_CHANNEL, kind: 'apply', ratio: 0.5 } });
  assert.equal(main.scrollTop, 750);
  assert.equal(root.scrollTop, 0);
  assert.equal(messages[messages.length - 1]?.top, 750);
  message({ source: parent, data: { channel: GT_SCROLL_CHANNEL, kind: 'apply', top: 250 } });
  assert.equal(main.scrollTop, 250);
});
