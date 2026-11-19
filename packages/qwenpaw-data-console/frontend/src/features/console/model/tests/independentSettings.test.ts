import assert from 'node:assert/strict';
import test from 'node:test';
import {
  activeModelSaveMode,
  hasSemanticModelSettings,
  saveActiveModelDraft,
  sessionModelPayload,
} from '../chat/activeModelSettings.js';
import type { ModelProvider } from '../../api/types.js';
import { cronChannelAvailability } from '../../../cron/model/cronChannelAvailability.js';

test('a session-only draft builds a complete Host update without CM or runtime settings', () => {
  const draft = { default_provider_id: 'local', default_model_id: 'chat' };
  assert.deepEqual(sessionModelPayload(draft), {
    ...draft,
    light_provider_id: null,
    light_model_id: null,
  });
  const values = { ...draft, light_selection: 'other/org/small', embedding_dim: 999 };
  assert.deepEqual(sessionModelPayload(values), {
    ...draft,
    light_provider_id: 'other',
    light_model_id: 'org/small',
  });
});

test('missing channel config leaves Console available but never enables an external channel', () => {
  for (const config of [undefined, {}, { feishu: { enabled: false } }]) {
    assert.deepEqual(
      [...cronChannelAvailability(config)].filter(([, enabled]) => enabled),
      [['console', true]],
    );
  }
  const configured = cronChannelAvailability({ wechat: { enabled: true } });
  assert.equal(configured.get('wechat'), true);
  assert.equal(configured.get('feishu'), false);
});

const offline = { loading: false, error: 'Not Found', testing: null, config: null };
const ready = { ...offline, error: null, config: {} };
const active = { default_provider_id: 'p', default_model_id: 'chat' };
const integrated = {
  ...active,
  cm_provider_id: 'p',
  cm_model_id: 'semantic',
  embedding_provider_id: 'p',
  embedding_model_id: 'embed',
  embedding_dim: 1024,
};
const provider: ModelProvider = {
  id: 'p',
  name: 'Local',
  configured: true,
  base_url: 'http://localhost:9010',
  catalog_base_url: '',
  chat_model: '',
  api_key_masked: '',
  api_key_prefix: '',
  models: [],
};

function recorder(fail?: string) {
  const calls: Record<string, unknown> = {};
  const write = (name: string) => async (body: unknown) => {
    calls[name] = body;
    if (name === fail) throw new Error('503');
  };
  return {
    calls,
    api: {
      updateActiveModels: write('session'),
      updateLLM: write('llm'),
      updateEmbedding: write('embedding'),
    },
  };
}

test('only explicit host-only capability permits saving without semantic fields', async () => {
  const { calls, api } = recorder();
  const mode = activeModelSaveMode(hasSemanticModelSettings(false), offline);
  assert.deepEqual(await saveActiveModelDraft(mode, active, [], api), []);
  assert.deepEqual(calls, { session: sessionModelPayload(active) });
});

test('integrated or unspecified deployments never fall back to a Host-only write on errors', async () => {
  for (const capability of [true, undefined]) {
    for (const bridge of [
      offline,
      { ...ready, loading: true },
      { ...ready, testing: 'llm' },
      { ...ready, config: null },
    ]) {
      const { calls, api } = recorder();
      const mode = activeModelSaveMode(hasSemanticModelSettings(capability), bridge);
      await assert.rejects(saveActiveModelDraft(mode, integrated, [provider], api), /unavailable/);
      assert.deepEqual(calls, {});
    }
  }
});

test('one integrated save dispatches all three updates and identifies failed writes', async () => {
  const mode = activeModelSaveMode(hasSemanticModelSettings(true), ready);
  assert.equal(mode, 'integrated');
  for (const fail of [undefined, 'embedding']) {
    const { calls, api } = recorder(fail);
    const errors = await saveActiveModelDraft(mode, integrated, [provider], api);
    assert.deepEqual(calls, {
      session: sessionModelPayload(integrated),
      llm: { provider_id: 'p', model: 'semantic', base_url: provider.base_url },
      embedding: { provider_id: 'p', model: 'embed', dim: 1024, base_url: provider.base_url },
    });
    assert.deepEqual(
      errors.map((error) => error.section),
      fail ? ['Embedding'] : [],
    );
  }
  const { calls, api } = recorder();
  await assert.rejects(saveActiveModelDraft(mode, active, [provider], api), /configured Provider/);
  assert.deepEqual(calls, {});
});
