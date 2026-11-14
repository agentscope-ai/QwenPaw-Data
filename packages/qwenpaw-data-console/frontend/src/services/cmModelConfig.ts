import { portalHeaders } from './auth.js';

// CM 侧 GET /api/system/model-config/ 返回体中的关键字段
interface CMModelConfigResponse {
  configured?: unknown;
}

export interface CMModelConfigStatus {
  // false: 无法确定（网络、CORS、鉴权等失败），此时不应拦截 chat
  determined: boolean;
  // 仅在 determined=true 时有效
  configured: boolean;
}

const MODEL_CONFIG_PATH = '/cm-api/system/model-config/';
const PROBE_TIMEOUT_MS = 5_000;

// 探测 CM 当前用户是否完成了 LLM + Embedding 配置；任意失败一律 fail-open。
export async function fetchCMModelConfigStatus(): Promise<CMModelConfigStatus> {
  if (typeof window === 'undefined') return { determined: false, configured: false };
  const url = MODEL_CONFIG_PATH;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: portalHeaders(url),
      signal: controller.signal,
    });
    if (!response.ok) return { determined: false, configured: false };
    const payload = (await response.json()) as CMModelConfigResponse;
    return { determined: true, configured: payload?.configured === true };
  } catch {
    return { determined: false, configured: false };
  } finally {
    window.clearTimeout(timer);
  }
}

// One gate per auth identity, shared by background probes, queue admission and dispatch.
// Session navigation must not reset a user's model configuration status.
export function createCMModelConfigGate(probe = fetchCMModelConfigStatus) {
  let required = false;
  let inFlight: Promise<boolean> | null = null;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => required,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    check: (): Promise<boolean> => {
      if (!inFlight) {
        inFlight = probe()
          .then((status) => {
            if (status.determined && required !== !status.configured) {
              required = !status.configured;
              listeners.forEach((listener) => listener());
            }
            return !status.determined || status.configured;
          })
          .finally(() => {
            inFlight = null;
          });
      }
      return inFlight;
    },
  };
}
