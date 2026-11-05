export type RuntimeMessageSyncMode = 'none' | 'replace';

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
}: {
  currentSessionId: string | undefined;
  nextSessionId: string | undefined;
  connectionStatus: string;
}): string | undefined {
  if (
    currentSessionId === 'new'
    && nextSessionId !== 'new'
    && ['connecting', 'streaming'].includes(connectionStatus)
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
  connectionStatus,
}: {
  previousSessionId: string | null;
  nextSessionId: string;
  wasGenerating: boolean;
  isGenerating: boolean;
  connectionStatus: string;
}): RuntimeMessageSyncMode {
  if (previousSessionId === nextSessionId) {
    return wasGenerating && !isGenerating ? 'replace' : 'none';
  }

  if (previousSessionId === 'new' && connectionStatus !== 'loading') {
    return 'none';
  }

  return 'replace';
}
