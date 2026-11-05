import { useCallback, useEffect, useRef, useState } from 'react';
import {
  settlementApi,
  type SettlementCard,
} from '../api/settlement';

const SETTLEMENT_POLL_INTERVAL_MS = 15_000;

type NewSettlementCardsHandler = (cards: SettlementCard[]) => void;

function isActionable(status: string): boolean {
  return status === 'pending' || status === 'queried';
}

function isProcessed(status: string): boolean {
  return status === 'confirmed' || status === 'dismissed';
}

function upsertCard(cards: SettlementCard[], nextCard: SettlementCard): SettlementCard[] {
  return [nextCard, ...cards.filter((card) => card.id !== nextCard.id)];
}

export function useSettlementCards(
  sessionId: string | null | undefined,
  onNewCards?: NewSettlementCardsHandler,
) {
  const [pendingCards, setPendingCards] = useState<SettlementCard[]>([]);
  const [processedCards, setProcessedCards] = useState<SettlementCard[]>([]);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [processedLoading, setProcessedLoading] = useState(false);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [processedError, setProcessedError] = useState<string | null>(null);
  const pendingRequestSequenceRef = useRef(0);
  const processedRequestSequenceRef = useRef(0);
  const sessionIdRef = useRef(sessionId);
  const onNewCardsRef = useRef(onNewCards);
  const seenCardKeysRef = useRef(new Set<string>());

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    onNewCardsRef.current = onNewCards;
  }, [onNewCards]);

  const refreshPending = useCallback(async () => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) {
      setPendingCards([]);
      setPendingError(null);
      setPendingLoading(false);
      return;
    }

    const requestSequence = ++pendingRequestSequenceRef.current;
    setPendingLoading(true);
    try {
      const result = await settlementApi.listCards(activeSessionId, 'pending');
      if (
        requestSequence !== pendingRequestSequenceRef.current
        || sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      const nextCards = (result.cards ?? []).filter((card) => isActionable(card.status));
      const newCards = nextCards.filter((card) => (
        !seenCardKeysRef.current.has(`${activeSessionId}:${card.id}`)
      ));
      nextCards.forEach((card) => {
        seenCardKeysRef.current.add(`${activeSessionId}:${card.id}`);
      });
      setPendingCards(nextCards);
      setPendingError(null);
      if (newCards.length) {
        onNewCardsRef.current?.(newCards);
      }
    } catch (fetchError) {
      if (
        requestSequence !== pendingRequestSequenceRef.current
        || sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      setPendingError(
        fetchError instanceof Error ? fetchError.message : String(fetchError),
      );
    } finally {
      if (requestSequence === pendingRequestSequenceRef.current) {
        setPendingLoading(false);
      }
    }
  }, []);

  const refreshProcessed = useCallback(async () => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) {
      setProcessedCards([]);
      setProcessedError(null);
      setProcessedLoading(false);
      return;
    }

    const requestSequence = ++processedRequestSequenceRef.current;
    setProcessedLoading(true);
    try {
      const result = await settlementApi.listCards(activeSessionId);
      if (
        requestSequence !== processedRequestSequenceRef.current
        || sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      setProcessedCards(
        (result.cards ?? []).filter((card) => isProcessed(card.status)),
      );
      setProcessedError(null);
    } catch (fetchError) {
      if (
        requestSequence !== processedRequestSequenceRef.current
        || sessionIdRef.current !== activeSessionId
      ) {
        return;
      }
      setProcessedError(
        fetchError instanceof Error ? fetchError.message : String(fetchError),
      );
    } finally {
      if (requestSequence === processedRequestSequenceRef.current) {
        setProcessedLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    pendingRequestSequenceRef.current += 1;
    processedRequestSequenceRef.current += 1;
    setPendingCards([]);
    setProcessedCards([]);
    setPendingError(null);
    setProcessedError(null);
    if (!sessionId) {
      setPendingLoading(false);
      setProcessedLoading(false);
      return undefined;
    }

    const refreshPendingIfVisible = () => {
      if (document.visibilityState === 'visible') {
        void refreshPending();
      }
    };

    refreshPendingIfVisible();
    void refreshProcessed();
    const intervalId = window.setInterval(
      refreshPendingIfVisible,
      SETTLEMENT_POLL_INTERVAL_MS,
    );
    document.addEventListener('visibilitychange', refreshPendingIfVisible);

    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', refreshPendingIfVisible);
    };
  }, [refreshPending, refreshProcessed, sessionId]);

  const confirm = useCallback(async (
    cardId: string,
    fields?: Record<string, string> | null,
  ) => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) return;

    const result = await settlementApi.confirmCard(activeSessionId, cardId, fields);
    if (sessionIdRef.current !== activeSessionId) return;
    pendingRequestSequenceRef.current += 1;
    setPendingLoading(false);
    setPendingCards((current) => current.filter((card) => card.id !== cardId));
    processedRequestSequenceRef.current += 1;
    setProcessedCards((current) => upsertCard(current, result.card));
    void refreshProcessed();
  }, [refreshProcessed]);

  const dismiss = useCallback(async (cardId: string) => {
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) return;

    const result = await settlementApi.dismissCard(activeSessionId, cardId);
    if (sessionIdRef.current !== activeSessionId) return;
    pendingRequestSequenceRef.current += 1;
    setPendingLoading(false);
    setPendingCards((current) => current.filter((card) => card.id !== cardId));
    processedRequestSequenceRef.current += 1;
    setProcessedCards((current) => upsertCard(current, result.card));
    void refreshProcessed();
  }, [refreshProcessed]);

  return {
    pendingCards,
    processedCards,
    pendingLoading,
    processedLoading,
    pendingError,
    processedError,
    actionableCount: pendingCards.length,
    refreshPending,
    refreshProcessed,
    confirm,
    dismiss,
  };
}
