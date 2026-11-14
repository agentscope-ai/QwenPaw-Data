import type { RuntimeSettings, ToolConfirmationLevel } from '../../api/types.js';

export function isToolConfirmationLevel(value: unknown): value is ToolConfirmationLevel {
  return value === 'off' || value === 'auto' || value === 'all';
}

export function normalizeRuntimeSettings(settings: RuntimeSettings): RuntimeSettings {
  return {
    ...settings,
    tool_confirmation_level: isToolConfirmationLevel(settings.tool_confirmation_level)
      ? settings.tool_confirmation_level
      : undefined,
  };
}

export function runtimeSettingsPatch(
  values: Partial<RuntimeSettings>,
  confirmationAvailable: boolean,
) {
  const result = { ...values };
  if (!confirmationAvailable || !isToolConfirmationLevel(result.tool_confirmation_level)) {
    delete result.tool_confirmation_level;
  }
  return result;
}

export async function saveRuntimeSettingsDraft(
  values: Partial<RuntimeSettings>,
  confirmationAvailable: boolean,
  handlers: {
    isCurrent: () => boolean;
    save: (body: Partial<RuntimeSettings>) => Promise<{ runtime_settings: RuntimeSettings }>;
    onSaved: (settings: RuntimeSettings) => void;
  },
) {
  if (!handlers.isCurrent()) return;
  const response = await handlers.save(runtimeSettingsPatch(values, confirmationAvailable));
  if (handlers.isCurrent()) handlers.onSaved(normalizeRuntimeSettings(response.runtime_settings));
}
