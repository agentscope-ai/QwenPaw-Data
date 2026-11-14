import { formatDetail } from '@/auth';
import type { SemanticConfigErrorBody } from '@/types/semanticConfig';
import { isSemanticConfigErrorBody } from '@/utils/semanticConfigContract';

type UnknownRecord = Record<string, unknown>;

export const EMBEDDING_REBUILD_IN_PROGRESS = 'embedding_rebuild_in_progress';
export const GATEWAY_UNAVAILABLE = 'gateway_unavailable';

export const REBUILD_IN_PROGRESS_MESSAGE = '图谱正在重建，其他功能暂时不可用，请稍后刷新重试';
export const SERVICE_UNAVAILABLE_MESSAGE = '服务暂时不可用，请稍后刷新重试';

export interface ApiErrorDetails {
  status: number;
  code?: string;
  body?: unknown;
  details?: UnknownRecord;
  retryAfter?: number;
  semanticConfig?: SemanticConfigErrorBody;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly body?: unknown;
  readonly details?: UnknownRecord;
  readonly retryAfter?: number;
  readonly semanticConfig?: SemanticConfigErrorBody;

  constructor(message: string, details: ApiErrorDetails) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = details.status;
    this.code = details.code;
    this.body = details.body;
    this.details = details.details;
    this.retryAfter = details.retryAfter;
    this.semanticConfig = details.semanticConfig;
  }
}

export function getErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function isFormValidationError(error: unknown): error is { errorFields: unknown[] } {
  return Boolean(
    error &&
    typeof error === 'object' &&
    'errorFields' in error &&
    Array.isArray((error as { errorFields?: unknown }).errorFields),
  );
}

function asRecord(value: unknown): UnknownRecord | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function isMarkupErrorBody(value: string): boolean {
  const trimmed = value.trimStart();
  return (
    /^<!DOCTYPE[\s>]/i.test(trimmed) ||
    /^<html[\s>]/i.test(trimmed) ||
    /<title>[\s\S]*?(503|502|504)[\s\S]*?<\/title>/i.test(trimmed)
  );
}

function isRebuildInProgressMessage(value: string | undefined): boolean {
  return Boolean(value && /embedding rebuild in progress/i.test(value));
}

function humanizeErrorMessage(
  raw: string | undefined,
  status: number,
  statusText: string,
  code?: string,
): { message: string; code?: string } {
  if (code === EMBEDDING_REBUILD_IN_PROGRESS || isRebuildInProgressMessage(raw)) {
    return {
      message: REBUILD_IN_PROGRESS_MESSAGE,
      code: EMBEDDING_REBUILD_IN_PROGRESS,
    };
  }
  if (raw && isMarkupErrorBody(raw)) {
    return { message: SERVICE_UNAVAILABLE_MESSAGE, code: GATEWAY_UNAVAILABLE };
  }
  if (raw) return { message: raw, code };
  if (status === 503) {
    return { message: SERVICE_UNAVAILABLE_MESSAGE, code: code ?? GATEWAY_UNAVAILABLE };
  }
  return {
    message: `Request failed: ${status}${statusText ? ` ${statusText}` : ''}`,
    code,
  };
}

/**
 * Normalize Auth, route-permission, Context envelope and semantic-config
 * errors into one stable error class.
 */
export function parseApiError(body: unknown, status: number, statusText = ''): ApiRequestError {
  const payload = asRecord(body);
  const detail = payload?.detail;
  const detailPayload = asRecord(detail);
  const nestedDetailError = asRecord(detailPayload?.error);
  const topLevelError = asRecord(payload?.error);
  const structuredDetails =
    asRecord(nestedDetailError?.details) ??
    asRecord(nestedDetailError?.detail) ??
    asRecord(topLevelError?.details) ??
    asRecord(topLevelError?.detail);

  const code =
    nonEmptyString(nestedDetailError?.code) ??
    nonEmptyString(topLevelError?.code) ??
    nonEmptyString(payload?.code);

  const detailMessage = nonEmptyString(formatDetail(detail));
  const rawMessage =
    nonEmptyString(nestedDetailError?.message) ??
    nonEmptyString(topLevelError?.message) ??
    detailMessage ??
    nonEmptyString(payload?.message) ??
    nonEmptyString(payload?.error) ??
    (typeof body === 'string' ? nonEmptyString(body) : undefined);
  const humanized = humanizeErrorMessage(rawMessage, status, statusText, code);

  const retryAfterValue = Number(payload?.retry_after);
  const retryAfter =
    Number.isFinite(retryAfterValue) && retryAfterValue > 0 ? retryAfterValue : undefined;
  const displayMessage =
    status === 429 && retryAfter ? `${humanized.message} (${retryAfter}s)` : humanized.message;

  return new ApiRequestError(displayMessage, {
    status,
    code: humanized.code,
    body,
    details: structuredDetails ?? undefined,
    retryAfter,
    semanticConfig: isSemanticConfigErrorBody(body) ? body : undefined,
  });
}

export async function apiErrorFromResponse(response: Response): Promise<ApiRequestError> {
  const contentType = response.headers.get('content-type') || '';
  const text = await response.text().catch(() => '');
  let body: unknown = text;
  if (contentType.includes('application/json') && text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  return parseApiError(body, response.status, response.statusText);
}
