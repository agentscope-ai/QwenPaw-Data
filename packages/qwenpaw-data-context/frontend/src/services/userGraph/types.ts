export type UserGraphMemoryType = 'principle' | 'preference' | 'anchored_rule' | 'trigger';

export type UserGraphMemoryStatus = 'active' | 'needs_review' | 'revoked';

export type UserGraphMemoryAction = 'created' | 'merged' | 'updated' | 'revoked';

export type UserGraphReferenceGraph = 'metadata' | 'knowledge';

export type UserGraphReferenceLabel = 'Metric' | 'Dimension' | 'Column' | 'Event' | 'Entity';

export interface UserGraphProfile {
  display_name: string | null;
  description: string | null;
  locale: string | null;
  timezone: string | null;
}

export interface UserGraphUser {
  workspace_id: string;
  user_id: string;
  profile: UserGraphProfile | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface UserGraphReferenceInput {
  graph: UserGraphReferenceGraph;
  label: UserGraphReferenceLabel;
  key: string;
}

export interface UserGraphReference extends UserGraphReferenceInput {
  name: string;
}

export interface UserGraphMemory {
  id: string;
  workspace_id: string;
  owner_id: string;
  type: UserGraphMemoryType;
  description: string;
  source_type: string;
  source_ref: string;
  source_refs: string[];
  triggers: string[];
  status: UserGraphMemoryStatus;
  created_at: string | null;
  updated_at: string | null;
  references: UserGraphReference[];
  action?: UserGraphMemoryAction | null;
}

export interface UserGraphPaginationMeta {
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface UserGraphEnvelope<T, TMeta = null> {
  ok: boolean;
  data: T | null;
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
    detail?: Record<string, unknown>;
  } | null;
  meta: TMeta;
}

export interface UserGraphPage<T> {
  items: T[];
  meta: UserGraphPaginationMeta;
}

export interface UserGraphUserListParams {
  page?: number;
  pageSize?: number;
}

export interface UserGraphProfilePatch {
  display_name?: string | null;
  description?: string | null;
  locale?: string | null;
  timezone?: string | null;
}

export interface UserGraphMemoryListParams {
  type?: UserGraphMemoryType;
  status?: UserGraphMemoryStatus;
  q?: string;
  page?: number;
  pageSize?: number;
}

export interface UserGraphMemoryCreate {
  type: UserGraphMemoryType;
  description: string;
  triggers?: string[];
  references?: UserGraphReferenceInput[];
}

export interface UserGraphMemoryPatch {
  type?: UserGraphMemoryType;
  description?: string;
  triggers?: string[];
  references?: UserGraphReferenceInput[];
}
