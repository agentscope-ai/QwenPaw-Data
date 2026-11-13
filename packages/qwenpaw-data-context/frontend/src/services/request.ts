import { buildAuthHeaders } from './authHeaders';
import { getApiUrl } from './config';
import { isEmbedded } from '../embedded/context';
import { ApiRequestError, apiErrorFromResponse } from './apiError';

export interface RequestOptions extends RequestInit {
  /** Route loads keep the legacy /403 redirect; in-page actions opt out. */
  redirectOnForbidden?: boolean;
}

function buildHeaders(method?: string, extra?: HeadersInit, body?: BodyInit | null): Headers {
  const headers = extra instanceof Headers ? new Headers(extra) : new Headers(extra);
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;

  if (
    method &&
    ['POST', 'PUT', 'PATCH'].includes(method.toUpperCase()) &&
    !isFormData &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json');
  }

  for (const [key, value] of Object.entries(buildAuthHeaders())) {
    if (!headers.has(key)) headers.set(key, value);
  }
  return headers;
}

async function redirectToTenantLogin(): Promise<void> {
  const { buildLocalLoginUrl } = await import('@/store/authStore');
  window.location.assign(buildLocalLoginUrl());
}

async function fetchApi(
  path: string,
  options: RequestOptions = {},
  authRetried = false,
): Promise<Response> {
  const { redirectOnForbidden = true, ...fetchOptions } = options;
  let response: Response;
  try {
    response = await fetch(getApiUrl(path), {
      ...fetchOptions,
      credentials: 'include',
      headers: buildHeaders(fetchOptions.method || 'GET', fetchOptions.headers, fetchOptions.body),
    });
  } catch (error) {
    throw new ApiRequestError(error instanceof Error ? error.message : 'Network request failed', {
      status: 0,
    });
  }

  if (!response.ok) {
    if (response.status === 401 && !authRetried) {
      const { refreshToken } = await import('@/auth');
      const refreshed = await refreshToken();
      if (refreshed) return fetchApi(path, options, true);
    }

    const error = await apiErrorFromResponse(response);

    if (response.status === 401) {
      await redirectToTenantLogin();
    }

    if (response.status === 403) {
      // Import the provider directly: materializing the design barrel namespace
      // in a split library bundle can leave external icon re-exports unbound.
      const { message } = await import('@agentscope-ai/design');
      message?.error(error.message);
      if (!isEmbedded() && redirectOnForbidden && window.location.pathname !== '/403') {
        window.location.assign('/403');
      }
    }

    throw error;
  }

  return response;
}

export async function requestBlob(path: string, options: RequestOptions = {}): Promise<Blob> {
  const response = await fetchApi(path, options);
  return response.blob();
}

export interface BlobDownloadResponse {
  blob: Blob;
  filename?: string;
}

export function parseContentDispositionFilename(
  contentDisposition: string | null,
): string | undefined {
  if (!contentDisposition) return undefined;

  const encodedMatch = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1].trim());
    } catch {
      return encodedMatch[1].trim();
    }
  }

  const filenameMatch = contentDisposition.match(/filename="?([^";]+)"?/i);
  return filenameMatch?.[1]?.trim() || undefined;
}

export async function requestBlobDownload(
  path: string,
  options: RequestOptions = {},
): Promise<BlobDownloadResponse> {
  const response = await fetchApi(path, options);
  return {
    blob: await response.blob(),
    filename: parseContentDispositionFilename(response.headers.get('content-disposition')),
  };
}

export async function request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetchApi(path, options);

  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    return (await response.text()) as T;
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiRequestError('Invalid JSON response', {
      status: response.status,
    });
  }
}
