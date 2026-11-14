import { getApiUrl } from './config.js';
import { fetchWithAuthRetry } from './auth.js';

interface ErrorPayload {
  code?: unknown;
  message?: unknown;
  details?: unknown;
  detail?: unknown;
  error?: unknown;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly details: Record<string, unknown> | null;

  constructor({
    status,
    message,
    code = null,
    details = null,
  }: {
    status: number;
    message: string;
    code?: string | null;
    details?: Record<string, unknown> | null;
  }) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function getErrorMessageFromBody(text: string, contentType: string): string | null {
  if (!text) return null;
  if (!contentType.includes('application/json')) return text;

  try {
    const payload = JSON.parse(text) as ErrorPayload;
    if (typeof payload.detail === 'string' && payload.detail) return payload.detail;
    if (typeof payload.message === 'string' && payload.message) return payload.message;
    if (typeof payload.error === 'string' && payload.error) return payload.error;
  } catch {
    return text;
  }

  return text;
}

function getErrorPayload(text: string, contentType: string): ErrorPayload | null {
  if (!text || !contentType.includes('application/json')) return null;
  try {
    const parsed = JSON.parse(text) as ErrorPayload;
    const detail =
      typeof parsed.detail === 'object' && parsed.detail !== null
        ? (parsed.detail as ErrorPayload)
        : parsed;
    return typeof detail.error === 'object' && detail.error !== null
      ? (detail.error as ErrorPayload)
      : detail;
  } catch {
    return null;
  }
}

function buildHeaders(method?: string, extra?: HeadersInit, body?: BodyInit | null): Headers {
  const headers = extra instanceof Headers ? extra : new Headers(extra);
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;

  if (method && ['POST', 'PUT', 'PATCH'].includes(method.toUpperCase()) && !isFormData) {
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
  }

  return headers;
}

export async function request<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  return requestAtUrl<T>(getApiUrl(path), options);
}

/** Request a resolved service URL without adding the Host API prefix. */
export async function requestAtUrl<T = unknown>(
  url: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await requestFileAtUrl(url, options);

  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    return (await response.text()) as T;
  }

  return (await response.json()) as T;
}

export async function requestFile(path: string, options: RequestInit = {}): Promise<Response> {
  return requestFileAtUrl(getApiUrl(path), options);
}

async function requestFileAtUrl(url: string, options: RequestInit): Promise<Response> {
  const response = await fetchWithAuthRetry(url, {
    ...options,
    headers: buildHeaders(options.method || 'GET', options.headers, options.body),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    const contentType = response.headers.get('content-type') || '';
    const payload = getErrorPayload(text, contentType);
    const errorMessage = getErrorMessageFromBody(text, contentType);
    throw new ApiRequestError({
      status: response.status,
      code: typeof payload?.code === 'string' ? payload.code : null,
      details:
        typeof payload?.details === 'object' && payload.details !== null
          ? (payload.details as Record<string, unknown>)
          : null,
      message:
        typeof payload?.message === 'string' && payload.message
          ? payload.message
          : errorMessage || `Request failed: ${response.status} ${response.statusText}`,
    });
  }

  return response;
}
