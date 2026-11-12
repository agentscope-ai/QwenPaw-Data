import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveHostModelConfigUrl } from './config.ts';

test('legacy model settings target only an explicit Host deployment and preserve its prefix', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'window');
  try {
    for (const [configured, expected] of [
      ['', ''],
      ['https://host.example', 'https://host.example/console/settings/agent'],
      ['https://host.example/console/', 'https://host.example/console/settings/agent'],
      ['/console', 'https://context.example/console/settings/agent'],
      [
        'https://host.example/product/console/?old=1#old',
        'https://host.example/product/console/settings/agent',
      ],
      ['javascript:alert(1)', ''],
    ]) {
      Object.defineProperty(globalThis, 'window', {
        configurable: true,
        value: {
          location: { origin: 'https://context.example' },
          __VITE_HOST_FRONTEND_URL__: configured,
        },
      });
      assert.equal(resolveHostModelConfigUrl(), expected, configured);
    }
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'window', descriptor);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});
