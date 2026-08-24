// ============================================================
// KG (Knowledge Graph) CRUD API Module
// ============================================================

import { cmRequest, cmRequestWithMeta, encodeKey, buildPageQuery } from "./helpers";
import type {
  EntityListItem,
  EntityListParams,
  EntityDetail,
  EntityCreatePayload,
  EntityUpdatePayload,
  EntityBatchDeletePayload,
  EventCreatePayload,
  EventUpdatePayload,
  EdgeRelatedToPayload,
  EdgeAboutPayload,
  EdgeCrossGraphPayload,
  EdgeCrossGraphDeletePayload,
  EdgePropertiesPayload,
  EdgeAdjacentDeletePayload,
  EdgeByTypeDeletePayload,
  RelTypeInfo,
} from "./types";

// ------------------------------------------------------------
// Entity CRUD
// ------------------------------------------------------------

/** GET /kg/entities — list entities with optional filters */
async function listEntities(params: EntityListParams = {}) {
  const query = buildPageQuery(params as Record<string, unknown>);
  const path = query ? `/kg/entities?${query}` : "/kg/entities";
  return cmRequestWithMeta<EntityListItem[]>(path);
}

/** GET /kg/entities/:key — get entity detail with neighbors */
async function getEntity(key: string, includeNeighbors = true, neighborLimit = 50) {
  const params: Record<string, unknown> = {
    include_neighbors: includeNeighbors,
    neighbor_limit: neighborLimit,
  };
  const query = buildPageQuery(params);
  return cmRequest<EntityDetail>(`/kg/entities/${encodeKey(key)}?${query}`);
}

/** PUT /kg/entities/:key — create or update entity */
async function createEntity(key: string, payload: EntityCreatePayload) {
  return cmRequest<void>(`/kg/entities/${encodeKey(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** PUT /kg/entities/:key — update entity */
async function updateEntity(key: string, payload: EntityUpdatePayload) {
  return cmRequest<void>(`/kg/entities/${encodeKey(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /kg/entities/:key — delete single entity */
async function deleteEntity(key: string) {
  return cmRequest<void>(`/kg/entities/${encodeKey(key)}`, {
    method: "DELETE",
  });
}

/** POST /kg/entities/batch-delete — batch delete entities */
async function batchDeleteEntities(payload: EntityBatchDeletePayload) {
  return cmRequest<void>("/kg/entities/batch-delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ------------------------------------------------------------
// Event CRUD
// ------------------------------------------------------------

/** GET /kg/events — list events with optional filters */
async function listEvents(params: Record<string, unknown> = {}) {
  const query = buildPageQuery(params);
  const path = query ? `/kg/events?${query}` : "/kg/events";
  return cmRequestWithMeta<EntityListItem[]>(path);
}

/** GET /kg/events/:key — get event detail */
async function getEvent(key: string, includeNeighbors = true) {
  const params: Record<string, unknown> = { include_neighbors: includeNeighbors };
  const query = buildPageQuery(params);
  return cmRequest<EntityDetail>(`/kg/events/${encodeKey(key)}?${query}`);
}

/** PUT /kg/events/:key — create or update event */
async function createEvent(key: string, payload: EventCreatePayload) {
  return cmRequest<void>(`/kg/events/${encodeKey(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** PUT /kg/events/:key — update event */
async function updateEvent(key: string, payload: EventUpdatePayload) {
  return cmRequest<void>(`/kg/events/${encodeKey(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /kg/events/:key — delete event */
async function deleteEvent(key: string) {
  return cmRequest<void>(`/kg/events/${encodeKey(key)}`, {
    method: "DELETE",
  });
}

// ------------------------------------------------------------
// Edge Management
// ------------------------------------------------------------

/** POST /kg/edges/related-to — create RELATED_TO edge */
async function createRelatedTo(payload: EdgeRelatedToPayload) {
  return cmRequest<void>("/kg/edges/related-to", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /kg/edges/related-to — delete RELATED_TO edge */
async function deleteRelatedTo(fromKey: string, toKey: string) {
  return cmRequest<void>("/kg/edges/related-to", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from_key: fromKey, to_key: toKey }),
  });
}

/** POST /kg/edges/about — connect or disconnect ABOUT edge */
async function manageAbout(payload: EdgeAboutPayload) {
  return cmRequest<void>("/kg/edges/about", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** POST /kg/edges/cross-graph — create cross-graph edge */
async function createCrossGraph(payload: EdgeCrossGraphPayload) {
  return cmRequest<void>("/kg/edges/cross-graph", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /kg/edges/cross-graph — delete cross-graph edge */
async function deleteCrossGraph(payload: EdgeCrossGraphDeletePayload) {
  return cmRequest<void>("/kg/edges/cross-graph", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /kg/edges/adjacent — delete edge between two specific nodes */
async function deleteAdjacent(payload: EdgeAdjacentDeletePayload) {
  return cmRequest<void>("/kg/edges/adjacent", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** PATCH /kg/edges/properties — update edge properties */
async function updateEdgeProperties(payload: EdgePropertiesPayload) {
  return cmRequest<{ from_key: string; to_key: string; rel_type: string; updated_properties: Record<string, unknown> }>(
    "/kg/edges/properties",
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

/** DELETE /kg/edges/by-type — batch delete edges by type from anchor */
async function deleteByType(payload: EdgeByTypeDeletePayload) {
  return cmRequest<void>("/kg/edges/by-type", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** GET /kg/edges/rel-types — get all relationship types */
async function getRelTypes() {
  return cmRequest<RelTypeInfo>("/kg/edges/rel-types");
}

// ------------------------------------------------------------
// Exported API object
// ------------------------------------------------------------

export const kgApi = {
  // Entity
  listEntities,
  getEntity,
  createEntity,
  updateEntity,
  deleteEntity,
  batchDeleteEntities,
  // Event
  listEvents,
  getEvent,
  createEvent,
  updateEvent,
  deleteEvent,
  // Edges
  createRelatedTo,
  deleteRelatedTo,
  manageAbout,
  createCrossGraph,
  deleteCrossGraph,
  deleteAdjacent,
  updateEdgeProperties,
  deleteByType,
  getRelTypes,
};
