// ============================================================
// CMGraph API — Topics Module (Topic Discovery)
// Backend: POST/GET /api/v1/admin/explorer/topics
// ============================================================

import { cmRequest } from './helpers';

export interface TopicMember {
  key: string;
  layer: 'MG' | 'KG' | 'UG';
  type: string;
  label: string;
  domain: string;
  /** The member's actual content (metric definition / knowledge / memory). */
  text: string;
}

export interface TopicEdge {
  s: string;
  t: string;
  /** Relation type for real graph edges; "" for semantic affinity. */
  rel: string;
  kind: 'graph' | 'semantic';
  sim?: number;
}

export interface Topic {
  id: string;
  title: string;
  summary: string;
  members: TopicMember[];
  edges: TopicEdge[];
  layer_counts: Partial<Record<'MG' | 'KG' | 'UG', number>>;
  size: number;
  /** false ⇒ degraded single-layer community (no cross-layer bridge). */
  cross_layer?: boolean;
}

export type TopicsReasonCode =
  'ok' | 'global_mg_scope' | 'single_layer_candidates' | 'no_cross_links' | 'cross_links_too_weak';

/** Structured emptiness/degradation diagnosis for the topic lens. */
export interface TopicsDiagnostics {
  layer_counts: Partial<Record<'MG' | 'KG' | 'UG', number>>;
  cross_semantic_edges: number;
  cross_hard_edges: number;
  max_cross_sim: number;
  reason_code: TopicsReasonCode;
}

export interface TopicsThresholds {
  intra: number;
  cross_floor: number;
  cross_top_k: number;
}

export interface TopicsResponse {
  status: 'ready' | 'computing' | 'missing' | 'error' | 'uninitialized' | 'initializing';
  topics: Topic[];
  computed_at?: number;
  elapsed_ms?: number;
  node_count?: number;
  embeddable_count?: number;
  merged_count?: number;
  sim_threshold?: number;
  thresholds?: TopicsThresholds;
  scope_operation?: string;
  diagnostics?: TopicsDiagnostics;
  workspace_id?: string;
  error?: string;
}

function postJson(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

/** Kick (or fetch cached) topic computation. wait=false returns immediately
 *  with status=computing when a fresh computation starts. */
async function computeTopics(
  params: {
    owner_id?: string;
    recompute?: boolean;
    wait?: boolean;
    lang?: string;
  } = {},
): Promise<TopicsResponse> {
  return cmRequest<TopicsResponse>('/explorer/topics', postJson(params));
}

/** Non-blocking cache/status read for polling. */
async function getTopics(owner_id = '', lang = ''): Promise<TopicsResponse> {
  const q = new URLSearchParams();
  if (owner_id) q.set('owner_id', owner_id);
  if (lang) q.set('lang', lang);
  const suffix = q.toString() ? `?${q}` : '';
  return cmRequest<TopicsResponse>(`/explorer/topics${suffix}`);
}

export const topicsApi = { computeTopics, getTopics };
