import { startVisiblePolling, sharePendingRequest } from '../polling/visiblePolling';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  skillProposalApi,
  type SkillProposal,
  type SkillProposalAcceptSelection,
} from '../../api/skillProposals';

const SKILL_PROPOSAL_POLL_INTERVAL_MS = 15_000;

export function useSkillProposals(sessionId: string | null | undefined) {
  const [pending, setPending] = useState<SkillProposal[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSequenceRef = useRef(0);
  const pendingSessionsRef = useRef(new Map<string, ReturnType<typeof skillProposalApi.list>>());
  const sessionIdRef = useRef(sessionId);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  const refreshPending = useCallback(async () => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) {
      setPending([]);
      setError(null);
      setLoading(false);
      return;
    }

    const requestSequence = ++requestSequenceRef.current;
    setLoading(true);
    try {
      const result = await sharePendingRequest(pendingSessionsRef.current, activeSessionId, () =>
        skillProposalApi.list(activeSessionId, 'pending'),
      );
      if (
        requestSequence !== requestSequenceRef.current ||
        sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      setPending(
        (result.skill_proposals ?? []).filter((proposal) => proposal.status === 'pending'),
      );
      setError(null);
    } catch (fetchError) {
      if (
        requestSequence !== requestSequenceRef.current ||
        sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      setError(fetchError instanceof Error ? fetchError.message : String(fetchError));
    } finally {
      if (requestSequence === requestSequenceRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    requestSequenceRef.current += 1;
    setPending([]);
    setError(null);
    if (!sessionId) {
      setLoading(false);
      return undefined;
    }

    const stop = startVisiblePolling(refreshPending, SKILL_PROPOSAL_POLL_INTERVAL_MS, document);
    return () => {
      stop();
      requestSequenceRef.current += 1;
    };
  }, [refreshPending, sessionId]);

  const accept = useCallback(
    async (proposalId: string, selection: SkillProposalAcceptSelection = {}) => {
      const activeSessionId = sessionIdRef.current;
      if (!activeSessionId) return undefined;

      const result = await skillProposalApi.accept(activeSessionId, proposalId, selection);
      if (sessionIdRef.current !== activeSessionId) return undefined;
      requestSequenceRef.current += 1;
      setLoading(false);
      setPending((current) => current.filter((proposal) => proposal.id !== proposalId));
      return result.skill_proposal;
    },
    [],
  );

  const reject = useCallback(async (proposalId: string) => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) return undefined;

    const result = await skillProposalApi.reject(activeSessionId, proposalId);
    if (sessionIdRef.current !== activeSessionId) return undefined;
    requestSequenceRef.current += 1;
    setLoading(false);
    setPending((current) => current.filter((proposal) => proposal.id !== proposalId));
    return result.skill_proposal;
  }, []);

  return {
    pending,
    loading,
    error,
    refreshPending,
    accept,
    reject,
  };
}
