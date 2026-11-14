import { ApiRequestError } from '../apiError';
import { request } from '../request';
import { useAuthStore } from '../../store/authStore';
import type {
  UserGraphEnvelope,
  UserGraphMemory,
  UserGraphMemoryAction,
  UserGraphMemoryCreate,
  UserGraphMemoryListParams,
  UserGraphMemoryPatch,
  UserGraphPage,
  UserGraphPaginationMeta,
  UserGraphProfilePatch,
  UserGraphUser,
  UserGraphUserListParams,
} from './types';

const USER_GRAPH_BASE = '/api/v1/ug';

function encoded(value: string): string {
  return encodeURIComponent(value);
}

// Mutations always target the authenticated subject. Explicit IDs serve admin reads.
function subjectPath(userId: string, write = false): string {
  const auth = useAuthStore.getState();
  if (write) {
    if (!auth.authEnabled || !auth.userId) {
      throw new ApiRequestError('Authentication required to edit memories', { status: 401 });
    }
    if (userId !== 'me' && userId !== auth.userId) {
      throw new ApiRequestError('Only your own memories can be edited', { status: 403 });
    }
    return 'me';
  }
  if (userId === 'me' || (auth.authEnabled && auth.userId && userId === auth.userId)) return 'me';
  return `users/${encoded(userId)}`;
}

function ensureData<T, TMeta>(envelope: UserGraphEnvelope<T, TMeta>, fallback: string): T {
  if (!envelope.ok || envelope.data == null) {
    throw new ApiRequestError(envelope.error?.message || fallback, {
      status: 200,
      code: envelope.error?.code,
      body: envelope,
      details: envelope.error?.details ?? envelope.error?.detail,
    });
  }
  return envelope.data;
}

function ensurePage<T>(
  envelope: UserGraphEnvelope<T[], UserGraphPaginationMeta | null>,
  fallback: string,
): UserGraphPage<T> {
  const items = ensureData(envelope, fallback);
  if (envelope.meta == null) {
    throw new ApiRequestError(`${fallback}: pagination metadata is missing`, {
      status: 200,
      body: envelope,
    });
  }
  return { items, meta: envelope.meta };
}

function ensureMemoryAction(
  memory: UserGraphMemory,
  allowed: readonly UserGraphMemoryAction[],
  fallback: string,
): UserGraphMemory {
  if (!memory.action || !allowed.includes(memory.action)) {
    throw new ApiRequestError(fallback, {
      status: 200,
      body: memory,
    });
  }
  return memory;
}

function appendPageParams(
  query: URLSearchParams,
  params: { page?: number; pageSize?: number },
): void {
  query.set('page', String(params.page ?? 1));
  query.set('page_size', String(params.pageSize ?? 20));
}

async function listUsers(
  params: UserGraphUserListParams = {},
): Promise<UserGraphPage<UserGraphUser>> {
  const auth = useAuthStore.getState();
  if (
    auth.authEnabled &&
    !auth.isPlatformAdmin &&
    !auth.capabilities.includes('tg_kg:others_read')
  ) {
    const user = await readUser('me');
    const page = params.page ?? 1;
    return {
      items: page === 1 && user ? [user] : [],
      meta: {
        page,
        page_size: params.pageSize ?? 20,
        total: user ? 1 : 0,
        has_more: false,
      },
    };
  }
  const query = new URLSearchParams();
  appendPageParams(query, params);
  const envelope = await request<
    UserGraphEnvelope<UserGraphUser[], UserGraphPaginationMeta | null>
  >(`${USER_GRAPH_BASE}/users?${query.toString()}`, {
    redirectOnForbidden: false,
  });
  return ensurePage(envelope, 'Failed to load User Graph users');
}

async function readUser(subject: string): Promise<UserGraphUser | null> {
  const envelope = await request<UserGraphEnvelope<UserGraphUser | UserGraphUser[]>>(
    `${USER_GRAPH_BASE}/${subject}`,
    { redirectOnForbidden: false },
  );
  const data = ensureData(envelope, 'Failed to load User Graph profile');
  if (Array.isArray(data)) {
    if (data.length === 0) return null;
    throw new ApiRequestError('Unexpected User Graph profile response', {
      status: 200,
      body: envelope,
    });
  }
  return data;
}

async function getUser(userId: string): Promise<UserGraphUser | null> {
  return readUser(subjectPath(userId));
}

async function patchUser(userId: string, body: UserGraphProfilePatch): Promise<UserGraphUser> {
  const envelope = await request<UserGraphEnvelope<UserGraphUser>>(
    `${USER_GRAPH_BASE}/${subjectPath(userId, true)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(body),
      redirectOnForbidden: false,
    },
  );
  return ensureData(envelope, 'Failed to update User Graph profile');
}

async function listMemories(
  userId: string,
  params: UserGraphMemoryListParams = {},
): Promise<UserGraphPage<UserGraphMemory>> {
  const query = new URLSearchParams();
  if (params.type) query.set('type', params.type);
  if (params.status) query.set('status', params.status);
  if (params.q?.trim()) query.set('q', params.q.trim());
  appendPageParams(query, params);
  const envelope = await request<
    UserGraphEnvelope<UserGraphMemory[], UserGraphPaginationMeta | null>
  >(`${USER_GRAPH_BASE}/${subjectPath(userId)}/memories?${query.toString()}`, {
    redirectOnForbidden: false,
  });
  return ensurePage(envelope, 'Failed to load User Graph memories');
}

async function createMemory(userId: string, body: UserGraphMemoryCreate): Promise<UserGraphMemory> {
  const envelope = await request<UserGraphEnvelope<UserGraphMemory>>(
    `${USER_GRAPH_BASE}/${subjectPath(userId, true)}/memories`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      redirectOnForbidden: false,
    },
  );
  const memory = ensureData(envelope, 'Failed to create User Graph memory');
  return ensureMemoryAction(memory, ['created', 'merged'], 'Unexpected User Graph create response');
}

async function getMemory(userId: string, memoryId: string): Promise<UserGraphMemory> {
  const envelope = await request<UserGraphEnvelope<UserGraphMemory>>(
    `${USER_GRAPH_BASE}/${subjectPath(userId)}/memories/${encoded(memoryId)}`,
    { redirectOnForbidden: false },
  );
  return ensureData(envelope, 'Failed to load User Graph memory');
}

async function patchMemory(
  userId: string,
  memoryId: string,
  body: UserGraphMemoryPatch,
): Promise<UserGraphMemory> {
  const envelope = await request<UserGraphEnvelope<UserGraphMemory>>(
    `${USER_GRAPH_BASE}/${subjectPath(userId, true)}/memories/${encoded(memoryId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(body),
      redirectOnForbidden: false,
    },
  );
  const memory = ensureData(envelope, 'Failed to update User Graph memory');
  return ensureMemoryAction(memory, ['updated'], 'Unexpected User Graph update response');
}

async function revokeMemory(userId: string, memoryId: string): Promise<UserGraphMemory> {
  const envelope = await request<UserGraphEnvelope<UserGraphMemory>>(
    `${USER_GRAPH_BASE}/${subjectPath(userId, true)}/memories/${encoded(memoryId)}`,
    {
      method: 'DELETE',
      redirectOnForbidden: false,
    },
  );
  const memory = ensureData(envelope, 'Failed to revoke User Graph memory');
  if (memory.status !== 'revoked') {
    throw new ApiRequestError('Unexpected User Graph revoke status', {
      status: 200,
      body: memory,
    });
  }
  return ensureMemoryAction(memory, ['revoked'], 'Unexpected User Graph revoke response');
}

export const userGraphApi = {
  listUsers,
  getUser,
  patchUser,
  listMemories,
  createMemory,
  getMemory,
  patchMemory,
  revokeMemory,
};
