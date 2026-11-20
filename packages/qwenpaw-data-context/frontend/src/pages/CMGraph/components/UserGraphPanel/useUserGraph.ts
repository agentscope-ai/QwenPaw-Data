import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiRequestError } from '@/services/apiError';
import { userGraphApi } from '@/services/userGraph';
import type {
  UserGraphMemory,
  UserGraphMemoryCreate,
  UserGraphMemoryPatch,
  UserGraphMemoryStatus,
  UserGraphMemoryType,
  UserGraphPaginationMeta,
  UserGraphProfilePatch,
  UserGraphUser,
} from '@/services/userGraph';

const OWNER_PAGE_SIZE = 100;
const MEMORY_PAGE_SIZE = 20;

export interface UserGraphMemoryQuery {
  type?: UserGraphMemoryType;
  status?: UserGraphMemoryStatus;
  q?: string;
  page: number;
  pageSize: number;
}

const DEFAULT_MEMORY_QUERY: UserGraphMemoryQuery = {
  page: 1,
  pageSize: MEMORY_PAGE_SIZE,
};

type MemoryMutation =
  { kind: 'create' } | { kind: 'update'; memoryId: string } | { kind: 'revoke'; memoryId: string };

interface UseUserGraphOptions {
  enabled: boolean;
  currentUserId: string;
  initialTargetUserId?: string;
  initialMemoryQuery?: Partial<UserGraphMemoryQuery>;
  canWriteOwn: boolean;
}

function prependUniqueUser(users: UserGraphUser[], user: UserGraphUser): UserGraphUser[] {
  return [user, ...users.filter((item) => item.user_id !== user.user_id)];
}

export function useUserGraph({
  enabled,
  currentUserId,
  initialTargetUserId,
  initialMemoryQuery,
  canWriteOwn,
}: UseUserGraphOptions) {
  const preferredTargetId = initialTargetUserId || currentUserId;
  const initialMemoryQueryRef = useRef<UserGraphMemoryQuery>({
    ...DEFAULT_MEMORY_QUERY,
    ...initialMemoryQuery,
  });
  const [targetUserId, setTargetUserId] = useState(preferredTargetId);
  const [owners, setOwners] = useState<UserGraphUser[]>([]);
  const [ownersMeta, setOwnersMeta] = useState<UserGraphPaginationMeta | null>(null);
  const [ownersLoading, setOwnersLoading] = useState(false);
  const [ownersError, setOwnersError] = useState<string | null>(null);
  const [user, setUser] = useState<UserGraphUser | null>(null);
  const [memories, setMemories] = useState<UserGraphMemory[]>([]);
  const [memoriesMeta, setMemoriesMeta] = useState<UserGraphPaginationMeta | null>(null);
  const [memoryQuery, setMemoryQuery] = useState<UserGraphMemoryQuery>(
    initialMemoryQueryRef.current,
  );
  const [memoriesLoading, setMemoriesLoading] = useState(false);
  const [memoriesError, setMemoriesError] = useState<string | null>(null);
  const [memoryMutation, setMemoryMutation] = useState<MemoryMutation | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetError, setTargetError] = useState<string | null>(null);
  // A User node only exists after the first offline distillation run, so the
  // "not created yet" case is tracked apart from real load failures.
  const [targetMissing, setTargetMissing] = useState(false);
  const [profileSaving, setProfileSaving] = useState(false);

  const ownersRequestTokenRef = useRef(0);
  const targetRequestTokenRef = useRef(0);
  const memoryRequestTokenRef = useRef(0);
  const targetUserIdRef = useRef(targetUserId);
  const memoryQueryRef = useRef(memoryQuery);
  targetUserIdRef.current = targetUserId;
  memoryQueryRef.current = memoryQuery;

  const changeTargetUser = useCallback(
    (nextTargetUserId: string) => {
      if (nextTargetUserId === targetUserIdRef.current) return;
      targetRequestTokenRef.current += 1;
      memoryRequestTokenRef.current += 1;
      targetUserIdRef.current = nextTargetUserId;
      setTargetUserId(nextTargetUserId);
      setUser(null);
      setMemories([]);
      setMemoriesMeta(null);
      memoryQueryRef.current = initialMemoryQueryRef.current;
      setMemoryQuery(initialMemoryQueryRef.current);
      setMemoriesError(null);
      setTargetError(null);
      setTargetMissing(false);
      setTargetLoading(Boolean(enabled && nextTargetUserId));
      setMemoriesLoading(Boolean(enabled && nextTargetUserId));
    },
    [enabled],
  );

  useEffect(() => {
    changeTargetUser(preferredTargetId);
  }, [changeTargetUser, preferredTargetId]);

  const loadOwners = useCallback(
    async (page = 1) => {
      if (!enabled) return;
      const requestToken = ownersRequestTokenRef.current + 1;
      ownersRequestTokenRef.current = requestToken;
      setOwnersLoading(true);
      setOwnersError(null);
      try {
        const result = await userGraphApi.listUsers({
          page,
          pageSize: OWNER_PAGE_SIZE,
        });
        if (ownersRequestTokenRef.current !== requestToken) return;
        setOwners((previous) => {
          if (page !== 1) {
            return [
              ...previous,
              ...result.items.filter(
                (item) => !previous.some((owner) => owner.user_id === item.user_id),
              ),
            ];
          }
          const selected = previous.find((owner) => owner.user_id === targetUserIdRef.current);
          return selected && !result.items.some((item) => item.user_id === selected.user_id)
            ? [selected, ...result.items]
            : result.items;
        });
        setOwnersMeta(result.meta);
        if (!targetUserIdRef.current && result.items[0]) {
          changeTargetUser(result.items[0].user_id);
        }
      } catch (error) {
        if (ownersRequestTokenRef.current !== requestToken) return;
        setOwnersError(error instanceof Error ? error.message : 'Failed to load users');
      } finally {
        if (ownersRequestTokenRef.current === requestToken) {
          setOwnersLoading(false);
        }
      }
    },
    [changeTargetUser, enabled],
  );

  const loadTarget = useCallback(
    async (nextTargetUserId: string) => {
      if (!enabled || !nextTargetUserId) return;
      const requestToken = targetRequestTokenRef.current + 1;
      targetRequestTokenRef.current = requestToken;
      const memoryRequestToken = memoryRequestTokenRef.current + 1;
      memoryRequestTokenRef.current = memoryRequestToken;
      setTargetLoading(true);
      setTargetError(null);
      setTargetMissing(false);
      setUser(null);
      setMemories([]);
      setMemoriesMeta(null);
      setMemoriesLoading(true);
      setMemoriesError(null);
      const isCurrentTargetRequest = () =>
        targetRequestTokenRef.current === requestToken &&
        targetUserIdRef.current === nextTargetUserId;
      const isCurrentMemoryRequest = () =>
        memoryRequestTokenRef.current === memoryRequestToken &&
        targetUserIdRef.current === nextTargetUserId;
      const userPromise = userGraphApi
        .getUser(nextTargetUserId)
        .then((nextUser) => {
          if (!isCurrentTargetRequest()) return;
          setUser(nextUser);
          if (nextUser) {
            setOwners((previous) => prependUniqueUser(previous, nextUser));
          }
        })
        .catch((error) => {
          if (!isCurrentTargetRequest()) return;
          // 404 means the User node has not been distilled yet — an expected
          // empty state, not a failure worth an error banner.
          if (error instanceof ApiRequestError && error.status === 404) {
            setTargetMissing(true);
            return;
          }
          setTargetError(error instanceof Error ? error.message : 'Failed to load User Graph');
        })
        .finally(() => {
          if (isCurrentTargetRequest()) setTargetLoading(false);
        });
      const memoryPromise = userGraphApi
        .listMemories(nextTargetUserId, memoryQueryRef.current)
        .then((memoryPage) => {
          if (!isCurrentMemoryRequest()) return;
          setMemories(memoryPage.items);
          setMemoriesMeta(memoryPage.meta);
        })
        .catch((error) => {
          if (!isCurrentMemoryRequest()) return;
          setMemoriesError(error instanceof Error ? error.message : 'Failed to load memories');
        })
        .finally(() => {
          if (isCurrentMemoryRequest()) setMemoriesLoading(false);
        });
      await Promise.all([userPromise, memoryPromise]);
    },
    [enabled],
  );

  const loadMemories = useCallback(
    async (query = memoryQueryRef.current, requestedTargetUserId = targetUserIdRef.current) => {
      if (!enabled || !requestedTargetUserId) return;
      const requestToken = memoryRequestTokenRef.current + 1;
      memoryRequestTokenRef.current = requestToken;
      setMemoriesLoading(true);
      setMemoriesError(null);
      try {
        let page = await userGraphApi.listMemories(requestedTargetUserId, query);
        if (
          memoryRequestTokenRef.current !== requestToken ||
          targetUserIdRef.current !== requestedTargetUserId
        )
          return;
        const lastPage = Math.max(1, Math.ceil(page.meta.total / query.pageSize));
        if (query.page > lastPage) {
          const adjustedQuery = { ...query, page: lastPage };
          memoryQueryRef.current = adjustedQuery;
          setMemoryQuery(adjustedQuery);
          page = await userGraphApi.listMemories(requestedTargetUserId, adjustedQuery);
          if (
            memoryRequestTokenRef.current !== requestToken ||
            targetUserIdRef.current !== requestedTargetUserId
          )
            return;
        }
        setMemories(page.items);
        setMemoriesMeta(page.meta);
      } catch (error) {
        if (
          memoryRequestTokenRef.current !== requestToken ||
          targetUserIdRef.current !== requestedTargetUserId
        )
          return;
        setMemoriesError(error instanceof Error ? error.message : 'Failed to load memories');
      } finally {
        if (
          memoryRequestTokenRef.current === requestToken &&
          targetUserIdRef.current === requestedTargetUserId
        )
          setMemoriesLoading(false);
      }
    },
    [enabled],
  );

  useEffect(() => {
    if (!enabled) return;
    void loadOwners(1);
  }, [enabled, loadOwners]);

  useEffect(() => {
    if (!enabled || !targetUserId) return;
    void loadTarget(targetUserId);
  }, [enabled, loadTarget, targetUserId]);

  useEffect(
    () => () => {
      ownersRequestTokenRef.current += 1;
      targetRequestTokenRef.current += 1;
      memoryRequestTokenRef.current += 1;
    },
    [],
  );

  const loadMoreOwners = useCallback(() => {
    if (ownersLoading || !ownersMeta?.has_more) return;
    void loadOwners(ownersMeta.page + 1);
  }, [loadOwners, ownersLoading, ownersMeta]);

  const selectTargetUser = useCallback(
    (nextTargetUserId: string) => {
      changeTargetUser(nextTargetUserId);
    },
    [changeTargetUser],
  );

  const retryTarget = useCallback(() => {
    if (targetUserId) void loadTarget(targetUserId);
  }, [loadTarget, targetUserId]);

  const updateMemoryQuery = useCallback(
    (patch: Partial<UserGraphMemoryQuery>, resetPage = true) => {
      const nextQuery: UserGraphMemoryQuery = {
        ...memoryQueryRef.current,
        ...patch,
        page: resetPage ? 1 : (patch.page ?? memoryQueryRef.current.page),
      };
      memoryQueryRef.current = nextQuery;
      setMemoryQuery(nextQuery);
      void loadMemories(nextQuery);
    },
    [loadMemories],
  );

  const changeMemoryPage = useCallback(
    (page: number, pageSize: number) => {
      const sizeChanged = pageSize !== memoryQueryRef.current.pageSize;
      updateMemoryQuery(
        {
          page: sizeChanged ? 1 : page,
          pageSize,
        },
        false,
      );
    },
    [updateMemoryQuery],
  );

  const refreshMemories = useCallback(async () => {
    await loadMemories(memoryQueryRef.current);
  }, [loadMemories]);

  const createMemory = useCallback(
    async (body: UserGraphMemoryCreate) => {
      const requestedTargetUserId = targetUserIdRef.current;
      if (!requestedTargetUserId) throw new Error('Target user is unavailable');
      setMemoryMutation({ kind: 'create' });
      try {
        const result = await userGraphApi.createMemory(requestedTargetUserId, body);
        if (targetUserIdRef.current === requestedTargetUserId) void refreshMemories();
        return result;
      } finally {
        setMemoryMutation(null);
      }
    },
    [refreshMemories],
  );

  const updateMemory = useCallback(
    async (memoryId: string, body: UserGraphMemoryPatch) => {
      const requestedTargetUserId = targetUserIdRef.current;
      if (!requestedTargetUserId) throw new Error('Target user is unavailable');
      setMemoryMutation({ kind: 'update', memoryId });
      try {
        const result = await userGraphApi.patchMemory(requestedTargetUserId, memoryId, body);
        if (targetUserIdRef.current === requestedTargetUserId) void refreshMemories();
        return result;
      } finally {
        setMemoryMutation(null);
      }
    },
    [refreshMemories],
  );

  const revokeMemory = useCallback(
    async (memoryId: string) => {
      const requestedTargetUserId = targetUserIdRef.current;
      if (!requestedTargetUserId) throw new Error('Target user is unavailable');
      setMemoryMutation({ kind: 'revoke', memoryId });
      try {
        const result = await userGraphApi.revokeMemory(requestedTargetUserId, memoryId);
        if (targetUserIdRef.current === requestedTargetUserId) void refreshMemories();
        return result;
      } finally {
        setMemoryMutation(null);
      }
    },
    [refreshMemories],
  );

  const getMemory = useCallback((memoryId: string) => {
    const requestedTargetUserId = targetUserIdRef.current;
    if (!requestedTargetUserId) throw new Error('Target user is unavailable');
    return userGraphApi.getMemory(requestedTargetUserId, memoryId);
  }, []);

  const updateProfile = useCallback(
    async (body: UserGraphProfilePatch) => {
      if (!targetUserId) throw new Error('Target user is unavailable');
      setProfileSaving(true);
      try {
        const updated = await userGraphApi.patchUser(targetUserId, body);
        if (targetUserIdRef.current === targetUserId) setUser(updated);
        setOwners((previous) => prependUniqueUser(previous, updated));
        return updated;
      } finally {
        setProfileSaving(false);
      }
    },
    [targetUserId],
  );

  const canWrite = useMemo(
    () => Boolean(targetUserId && targetUserId === currentUserId && canWriteOwn),
    [canWriteOwn, currentUserId, targetUserId],
  );

  return {
    targetUserId,
    selectTargetUser,
    owners,
    ownersMeta,
    ownersLoading,
    ownersError,
    loadMoreOwners,
    reloadOwners: () => loadOwners(1),
    user,
    memories,
    memoriesMeta,
    memoryQuery,
    memoriesLoading,
    memoriesError,
    updateMemoryQuery,
    changeMemoryPage,
    refreshMemories,
    memoryMutation,
    createMemory,
    updateMemory,
    revokeMemory,
    getMemory,
    targetLoading,
    targetError,
    targetMissing,
    retryTarget,
    canWrite,
    profileSaving,
    updateProfile,
  };
}
