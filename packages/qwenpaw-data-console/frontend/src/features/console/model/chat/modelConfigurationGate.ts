import type { ActiveModels, ModelProvider } from '../../api/types.js';
import type { ModelConfigResponse } from '../../api/modelConfigTypes.js';

export class ModelConfigurationRequiredError extends Error {
  constructor(readonly missing: readonly string[]) {
    super('Model configuration is incomplete');
    this.name = 'ModelConfigurationRequiredError';
  }
}

export function missingModelConfiguration(
  active: ActiveModels,
  providers: ModelProvider[],
  cm: ModelConfigResponse,
): string[] {
  const missing: string[] = [];
  const provider = providers.find((item) => item.id === active.default_provider_id);
  if (!provider?.configured) missing.push('main.provider');
  if (
    !active.default_model_id ||
    !provider?.models.some((item) => item.id === active.default_model_id)
  )
    missing.push('main.model');
  if (!cm.configured || cm.key_status !== 'ok')
    missing.push(...(cm.missing_fields?.length ? cm.missing_fields : ['DataBridge']));
  // Legacy valid CM configurations remain usable until the user explicitly selects shared Providers.
  for (const [channel, config] of [
    ['llm', cm.llm],
    ['embedding', cm.embedding],
  ] as const) {
    if (
      config.provider_id &&
      !providers.some((item) => item.id === config.provider_id && item.configured)
    )
      missing.push(`${channel}.provider`);
  }
  return missing;
}

interface ConfigurationSources {
  active: () => Promise<ActiveModels>;
  providers: () => Promise<ModelProvider[]>;
  context: () => Promise<ModelConfigResponse>;
}

/** Read fresh state at both input admission and queue execution; reject obsolete results. */
export async function checkModelConfiguration(
  sources: ConfigurationSources,
  assertCurrent: () => void,
): Promise<void> {
  assertCurrent();
  try {
    const [active, providers, cm] = await Promise.all([
      sources.active(),
      sources.providers(),
      sources.context(),
    ]);
    assertCurrent();
    const missing = missingModelConfiguration(active, providers, cm);
    if (missing.length) throw new ModelConfigurationRequiredError(missing);
  } catch (cause) {
    assertCurrent();
    throw cause;
  }
}

/** A one-shot receipt for immediate dispatch, never for queued execution. */
export function createModelConfigurationAdmission(scope: string, now = Date.now) {
  const expires = now() + 10_000;
  let consumed = false;
  return {
    consume(currentScope: string) {
      const accepted = !consumed && currentScope === scope && now() < expires;
      consumed = true;
      return accepted;
    },
  };
}
export type ModelConfigurationAdmission = ReturnType<typeof createModelConfigurationAdmission>;
