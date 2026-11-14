import { ApiRequestError } from './apiError';
import type { ApiResponse } from './cmgraph/types';
import { request } from './request';

export interface RawCypherRequest {
  cypher: string;
  params: Record<string, unknown>;
  limit: number;
}

export interface RawCypherResponse {
  rows: Record<string, unknown>[];
  count: number;
  columns: string[];
  elapsed_ms: number;
}

/**
 * Grant digests are byte-sensitive. Both issuance and execution must preserve
 * the exact textarea value, including leading/trailing whitespace.
 */
export function preserveRawCypherText(cypher: string): string {
  return cypher;
}

async function execute(
  body: RawCypherRequest,
  breakglassToken: string,
): Promise<RawCypherResponse> {
  const envelope = await request<ApiResponse<RawCypherResponse>>('/v1/admin/cypher/raw', {
    method: 'POST',
    headers: {
      'X-QwenPaw-Data-Breakglass-Token': breakglassToken,
    },
    body: JSON.stringify({
      cypher: preserveRawCypherText(body.cypher),
      params: body.params,
      limit: body.limit,
    }),
    redirectOnForbidden: false,
  });
  if (!envelope.ok || envelope.data == null) {
    throw new ApiRequestError(envelope.error?.message || 'Raw Cypher request failed', {
      status: 200,
      code: envelope.error?.code,
      body: envelope,
    });
  }
  return envelope.data;
}

export const rawCypherApi = { execute };
