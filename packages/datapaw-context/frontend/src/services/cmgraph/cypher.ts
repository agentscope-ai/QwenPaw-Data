import { cmRequest } from "./helpers";
import type { CypherRequest, CypherResponse } from "./types";

/**
 * Execute a read-only Cypher query (multi-view format).
 * POST /api/v1/admin/cypher
 */
async function execute(req: CypherRequest): Promise<CypherResponse> {
  return cmRequest<CypherResponse>("/cypher", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export const cypherApi = { execute };
