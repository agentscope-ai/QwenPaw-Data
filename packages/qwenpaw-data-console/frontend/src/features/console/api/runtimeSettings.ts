import { request } from '../../../services/request.js';
import type { RuntimeSettings } from './types.js';

export function getRuntimeSettings() {
  return request<{ runtime_settings: RuntimeSettings }>('/preferences/runtime-settings');
}

export function updateRuntimeSettings(body: Partial<RuntimeSettings>) {
  return request<{ runtime_settings: RuntimeSettings }>('/preferences/runtime-settings', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}
