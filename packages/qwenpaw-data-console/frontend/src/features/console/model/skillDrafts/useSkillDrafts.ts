import { startVisiblePolling, sharePendingRequest } from '../polling/visiblePolling';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { skillDraftsApi, type SkillDraft } from '../../api/skillDrafts';

const POLL_INTERVAL_MS = 3_000;

export function isSkillDraftBusy(status: string): boolean {
  return status === 'generating' || status === 'revising';
}

export function useSkillDrafts(sessionId: string | null | undefined, refreshKey?: number) {
  const [drafts, setDrafts] = useState<SkillDraft[]>([]);
  const [loading, setLoading] = useState(Boolean(sessionId));
  const [error, setError] = useState<string | null>(null);
  const requestSequenceRef = useRef(0);
  const pendingSessionsRef = useRef(new Map<string, ReturnType<typeof skillDraftsApi.list>>());

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setDrafts([]);
      setError(null);
      setLoading(false);
      return;
    }

    const requestSequence = ++requestSequenceRef.current;
    setLoading(true);
    try {
      const result = await sharePendingRequest(pendingSessionsRef.current, sessionId, () =>
        skillDraftsApi.list(sessionId),
      );
      if (requestSequence !== requestSequenceRef.current) return;
      setDrafts(result.skill_drafts ?? []);
      setError(null);
    } catch (fetchError) {
      if (requestSequence !== requestSequenceRef.current) return;
      setError(fetchError instanceof Error ? fetchError.message : String(fetchError));
    } finally {
      if (requestSequence === requestSequenceRef.current) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    requestSequenceRef.current += 1;
    setDrafts([]);
    setError(null);
    setLoading(Boolean(sessionId));
    void refresh();
    return () => {
      requestSequenceRef.current += 1;
    };
  }, [refresh, refreshKey, sessionId]);

  const hasBusyDraft = useMemo(
    () => drafts.some((draft) => isSkillDraftBusy(draft.status)),
    [drafts],
  );

  useEffect(() => {
    if (!sessionId || !hasBusyDraft) return undefined;
    return startVisiblePolling(refresh, POLL_INTERVAL_MS, document, false);
  }, [hasBusyDraft, refresh, sessionId]);

  return { drafts, loading, error, refresh };
}
