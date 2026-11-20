export type RuntimeMessageSyncMode = 'none' | 'replace';

export interface RuntimeTurnMessageIds {
  requestMessageId: string;
  responseMessageId: string;
}

export function restoreRuntimeRequest<T extends { id: string }>(
  mounted: T[],
  request: T,
  { requestMessageId, responseMessageId }: RuntimeTurnMessageIds,
): T[] {
  if (mounted.some(({ id }) => id === request.id || id === requestMessageId)) return mounted;
  const responseIndex = mounted.findIndex(({ id }) => id === responseMessageId);
  const insertAt = responseIndex === -1 ? mounted.length : responseIndex;
  return [
    ...mounted.slice(0, insertAt),
    { ...request, id: requestMessageId },
    ...mounted.slice(insertAt),
  ];
}

export interface BackgroundRuntimeMessageReconciliation<T extends { id: string }> {
  removedMessageIds: string[];
  trackedMessageIds: Set<string>;
  upsertMessages: T[];
}

export function reconcileBackgroundRuntimeMessages<T extends { id: string }>({
  currentMessages,
  nextMessages,
  trackedMessageIds,
}: {
  currentMessages: readonly T[];
  nextMessages: readonly T[];
  trackedMessageIds: ReadonlySet<string>;
}): BackgroundRuntimeMessageReconciliation<T> {
  const currentMessageIds = new Set(currentMessages.map((message) => message.id));
  const nextMessageIds = new Set(nextMessages.map((message) => message.id));
  const nextTrackedMessageIds = new Set(trackedMessageIds);
  const removedMessageIds = [...trackedMessageIds].filter(
    (messageId) => !nextMessageIds.has(messageId),
  );
  removedMessageIds.forEach((messageId) => nextTrackedMessageIds.delete(messageId));

  const lastMessageIndex = nextMessages.length - 1;
  const upsertMessages = nextMessages.filter((message, index) => {
    if (!currentMessageIds.has(message.id)) {
      nextTrackedMessageIds.add(message.id);
      return true;
    }
    return nextTrackedMessageIds.has(message.id) || index === lastMessageIndex;
  });

  return {
    removedMessageIds,
    trackedMessageIds: nextTrackedMessageIds,
    upsertMessages,
  };
}

export function selectRuntimeSessionMessages<T>({
  snapshotMessages,
  mountedMessages,
  snapshotSessionId,
  mountedSessionId,
  hasQueuedInputs,
}: {
  snapshotMessages: readonly T[];
  mountedMessages: readonly T[] | undefined;
  snapshotSessionId: string;
  mountedSessionId: string | undefined;
  hasQueuedInputs: boolean;
}): readonly T[] {
  if (hasQueuedInputs && mountedMessages && snapshotSessionId === mountedSessionId) {
    return mountedMessages;
  }
  return snapshotMessages;
}

export function resolveRuntimeInputQueueSessionId({
  runtimeSessionId,
  persistedSessionId,
  draftQueueSessionId,
}: {
  runtimeSessionId: string | undefined;
  persistedSessionId: string | undefined;
  draftQueueSessionId: string;
}): string {
  // Once the host has created a real Session, keep the queue on that stable
  // identity even while the Runtime UI still renders its optimistic `new`
  // Session. Otherwise queued inputs are stranded when `new` is replaced.
  if (persistedSessionId) return persistedSessionId;
  if (!runtimeSessionId || runtimeSessionId === 'new') return draftQueueSessionId;
  return runtimeSessionId;
}

export function selectControlledRuntimeSessionId({
  currentSessionId,
  nextSessionId,
  connectionStatus,
  hasPendingInput = false,
}: {
  currentSessionId: string | undefined;
  nextSessionId: string | undefined;
  connectionStatus: string;
  hasPendingInput?: boolean;
}): string | undefined {
  if (
    currentSessionId === 'new' &&
    nextSessionId !== 'new' &&
    (hasPendingInput || ['connecting', 'streaming'].includes(connectionStatus))
  ) {
    return currentSessionId;
  }

  return nextSessionId;
}

export function selectRuntimeMessageSyncMode({
  previousSessionId,
  nextSessionId,
  wasGenerating,
  isGenerating,
  hasQueuedInputs = false,
}: {
  previousSessionId: string | null;
  nextSessionId: string;
  wasGenerating: boolean;
  isGenerating: boolean;
  hasQueuedInputs?: boolean;
}): RuntimeMessageSyncMode {
  // The SDK session loader owns initial hydration and session changes.
  return previousSessionId === nextSessionId && wasGenerating && !isGenerating && !hasQueuedInputs
    ? 'replace'
    : 'none';
}

// Prepend older snapshot cards without dropping optimistic/queued SDK messages.
export function mergeHydratedRuntimeMessages<T extends { id: string }>({
  mounted,
  snapshot,
  protectedIds,
  liveTurns,
}: {
  mounted: readonly T[];
  snapshot: readonly T[];
  protectedIds: ReadonlySet<string>;
  liveTurns?: ReadonlyMap<string, RuntimeTurnMessageIds>;
}): T[] {
  const current = new Map(mounted.map((message) => [message.id, message]));
  const aliases = new Map<string, string>();
  liveTurns?.forEach(({ requestMessageId, responseMessageId }, chatId) => {
    if (!current.has(requestMessageId) && !current.has(responseMessageId)) return;
    aliases.set(`request-${chatId}`, requestMessageId);
    aliases.set(`assistant-${chatId}`, responseMessageId);
  });
  const known = new Set<string>();
  return [
    ...snapshot.map((message) => {
      const alias = aliases.get(message.id);
      known.add(message.id);
      if (alias) {
        known.add(alias);
        return current.get(alias) ?? { ...message, id: alias };
      }
      return protectedIds.has(message.id) ? (current.get(message.id) ?? message) : message;
    }),
    ...mounted.filter((message) => !known.has(message.id)),
  ];
}

export function shouldHydrateRuntimeHistory(
  previous: { mountKey: string; version: number } | null,
  next: { mountKey: string; version: number },
): boolean {
  return (
    previous !== null && previous.mountKey === next.mountKey && next.version > previous.version
  );
}
