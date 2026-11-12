import assert from 'node:assert/strict';
import test from 'node:test';
import {
  checkModelConfiguration,
  missingModelConfiguration,
  ModelConfigurationRequiredError,
} from '../chat/modelConfigurationGate.js';
import {
  buildAgentConfigurationPath,
  getAgentSettingsReturnPath,
} from '../../../../shared/navigation/sessionNavigation.js';
import type { ActiveModels, ModelProvider } from '../../api/types.js';
import type { ModelConfigResponse } from '../../api/modelConfigTypes.js';
const active: ActiveModels = {
  default_provider_id: 'p',
  default_model_id: 'm',
  light_provider_id: null,
  light_model_id: null,
};
const provider: ModelProvider = {
  id: 'p',
  name: 'P',
  configured: true,
  base_url: 'https://example.com/v1',
  catalog_base_url: '',
  api_key_masked: '***',
  api_key_prefix: '',
  chat_model: '',
  models: [{ id: 'm' } as ModelProvider['models'][number]],
};
const cm: ModelConfigResponse = {
  configured: true,
  key_status: 'ok',
  seeded: false,
  llm: { provider_id: 'p' },
  embedding: { provider_id: 'p' },
  embedding_space: { adopted: false, aligned: null },
};
test('main, DataBridge and embedding are required together; light model is optional', () => {
  assert.deepEqual(missingModelConfiguration(active, [provider], cm), []);
  assert.deepEqual(
    missingModelConfiguration(active, [provider], {
      ...cm,
      configured: false,
      missing_fields: ['embedding.dim'],
    }),
    ['embedding.dim'],
  );
  assert.ok(
    missingModelConfiguration(active, [{ ...provider, configured: false }], cm).includes(
      'main.provider',
    ),
  );
  assert.ok(
    missingModelConfiguration({ ...active, default_model_id: 'removed' }, [provider], cm).includes(
      'main.model',
    ),
  );
  assert.ok(
    missingModelConfiguration(active, [provider], { ...cm, key_status: 'undecryptable' }).length,
  );
});
test('CM references cannot point to removed Providers; valid legacy configurations stay usable', () => {
  assert.deepEqual(
    missingModelConfiguration(active, [provider], { ...cm, embedding: { provider_id: 'deleted' } }),
    ['embedding.provider'],
  );
  assert.deepEqual(
    missingModelConfiguration(active, [provider], { ...cm, llm: {}, embedding: {} }),
    [],
  );
});
test('settings return stays inside Host and preserves the original session and datasource', () => {
  const original = '/console?session_id=a%2Fb&datasource_id=x';
  const settings = buildAgentConfigurationPath(original);
  assert.equal(getAgentSettingsReturnPath(settings.slice(settings.indexOf('?'))), original);
  assert.equal(getAgentSettingsReturnPath('?return_to=https%3A%2F%2Fevil.example'), null);
  assert.equal(getAgentSettingsReturnPath('?return_to=%2F%2Fevil.example'), null);
  assert.equal(buildAgentConfigurationPath(), '/console/settings/agent');
});

test('execution checks again after queue admission, so a removed configuration cannot be reused', async () => {
  let configured = true;
  let reads = 0;
  const sources = {
    active: async () => active,
    providers: async () => [provider],
    context: async () => {
      reads++;
      return { ...cm, configured };
    },
  };
  await checkModelConfiguration(sources, () => {});
  configured = false;
  await assert.rejects(
    checkModelConfiguration(sources, () => {}),
    ModelConfigurationRequiredError,
  );
  assert.equal(reads, 2);
});
test('lookup failures keep their identity and obsolete workspace results never permit a send', async () => {
  const networkError = new Error('service unavailable');
  const sources = {
    active: async () => active,
    providers: async () => [provider],
    context: async () => cm,
  };
  await assert.rejects(
    checkModelConfiguration(
      {
        ...sources,
        context: async () => {
          throw networkError;
        },
      },
      () => {},
    ),
    (error) => error === networkError,
  );
  let current = true;
  let finish!: (value: ModelConfigResponse) => void;
  const request = checkModelConfiguration(
    {
      ...sources,
      context: () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    },
    () => {
      if (!current) throw new DOMException('Workspace changed', 'AbortError');
    },
  );
  current = false;
  finish(cm);
  await assert.rejects(request, { name: 'AbortError' });
});
