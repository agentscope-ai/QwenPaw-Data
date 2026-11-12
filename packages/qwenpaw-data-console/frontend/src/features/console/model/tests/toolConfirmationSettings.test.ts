import assert from 'node:assert/strict';
import test from 'node:test';
import type { RuntimeSettings } from '../../api/types.js';
import { getRuntimeSettings, updateRuntimeSettings } from '../../api/runtimeSettings.js';
import {
  isToolConfirmationLevel,
  normalizeRuntimeSettings,
  runtimeSettingsPatch,
  saveRuntimeSettingsDraft,
} from '../chat/toolConfirmationSettings.js';

const settings: RuntimeSettings = {
  tool_timeout_enabled: true,
  tool_timeout_seconds: 1800,
  react_max_iters: 30,
  llm_retry_enabled: true,
  llm_max_retries: 3,
  context_trigger_ratio: 0.75,
  context_reserve_ratio: 0.2,
  tool_result_limit: 5000,
  tool_confirmation_level: 'auto',
};

test('all three server levels round-trip; missing/unknown values remain unavailable, never defaulted', () => {
  for (const level of ['off', 'auto', 'all'] as const) {
    assert.equal(isToolConfirmationLevel(level), true);
    assert.equal(
      normalizeRuntimeSettings({ ...settings, tool_confirmation_level: level })
        .tool_confirmation_level,
      level,
    );
    assert.equal(
      runtimeSettingsPatch({ tool_confirmation_level: level }, true).tool_confirmation_level,
      level,
    );
  }
  for (const level of [undefined, null, 'future', false]) {
    const raw = { ...settings, tool_confirmation_level: level } as unknown as RuntimeSettings;
    assert.equal(normalizeRuntimeSettings(raw).tool_confirmation_level, undefined);
    assert.equal('tool_confirmation_level' in runtimeSettingsPatch(raw, true), false);
  }
});

test('unavailable confirmation is omitted, other fields preserved and context settings remain partial', () => {
  const draft = {
    tool_confirmation_level: 'all' as const,
    tool_timeout_seconds: 900,
    llm_max_retries: 4,
  };
  assert.deepEqual(runtimeSettingsPatch(draft, false), {
    tool_timeout_seconds: 900,
    llm_max_retries: 4,
  });
  assert.deepEqual(runtimeSettingsPatch({ context_trigger_ratio: 0.8 }, true), {
    context_trigger_ratio: 0.8,
  });
  assert.equal(draft.tool_confirmation_level, 'all');
});

test('settings read/save use the actual Host API and preserve server return values', async () => {
  const original = globalThis.fetch;
  const requests: RequestInit[] = [];
  globalThis.fetch = async (input, init) => {
    assert.equal(input, '/api/preferences/runtime-settings');
    assert.equal(init?.credentials, 'include');
    requests.push(init!);
    return Response.json({ runtime_settings: settings });
  };
  try {
    assert.deepEqual((await getRuntimeSettings()).runtime_settings, settings);
    assert.deepEqual(
      (await updateRuntimeSettings({ tool_confirmation_level: 'all' })).runtime_settings,
      settings,
    );
    assert.equal(requests[1].method, 'PUT');
    assert.deepEqual(JSON.parse(String(requests[1].body)), { tool_confirmation_level: 'all' });
  } finally {
    globalThis.fetch = original;
  }
});

test('save failure preserves draft and never calls the successful form update', async () => {
  const draft = { tool_confirmation_level: 'all' as const };
  await assert.rejects(
    saveRuntimeSettingsDraft(draft, true, {
      isCurrent: () => true,
      save: async () => {
        throw new Error('unavailable');
      },
      onSaved: () => assert.fail('cannot report saved'),
    }),
  );
  assert.equal(draft.tool_confirmation_level, 'all');
});

test('late settings responses cannot overwrite another identity or page generation', async () => {
  let current = true;
  let resolve!: (response: { runtime_settings: RuntimeSettings }) => void;
  const pending = saveRuntimeSettingsDraft({ tool_confirmation_level: 'all' }, true, {
    isCurrent: () => current,
    save: () =>
      new Promise((done) => {
        resolve = done;
      }),
    onSaved: () => assert.fail('stale form update'),
  });
  current = false;
  resolve({ runtime_settings: settings });
  await pending;
  await saveRuntimeSettingsDraft({}, true, {
    isCurrent: () => false,
    save: async () => {
      assert.fail('stale send');
    },
    onSaved: () => assert.fail('stale update'),
  });
});

test('successful save fills server-normalized values rather than claiming the selected value took effect', async () => {
  let saved: RuntimeSettings | undefined;
  await saveRuntimeSettingsDraft({ tool_confirmation_level: 'all' }, true, {
    isCurrent: () => true,
    save: async () => ({ runtime_settings: settings }),
    onSaved: (value) => {
      saved = value;
    },
  });
  assert.equal(saved?.tool_confirmation_level, 'auto');
});
