import { getApiUrl } from './config.js';

// Credentials stay in the loopback Python gateway; the browser never receives
// the Host Bearer token. This header is also required for SSE and file fetches.
export async function fetchWithAuthRetry(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('X-QwenPaw-Console', '1');
  return fetch(input, { ...init, headers, credentials: 'same-origin' });
}

export async function bootstrapHostAuth(): Promise<boolean> {
  const response = await fetchWithAuthRetry(getApiUrl('/auth/status'));
  if (!response.ok) throw new Error(`Host connection failed: ${response.status}`);
  return true;
}
