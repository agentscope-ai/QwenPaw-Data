import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { getHostCurrentUser } from '@/services/auth';
import { settlementApi, type SettlementCard } from '../../api/settlement';
import {
  skillProposalApi,
  type SkillProposal,
  type SkillProposalAcceptSelection,
} from '../../api/skillProposals';
import { getSettlementScope } from './settlementCenter';
import { createSettlementCenterStore } from './settlementCenterStore';
import {
  countUnreadSettlementCards,
  markSettlementVisited,
  readSettlementVisit,
} from './settlementReadState';

function visitStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function useSettlementCenter(active: boolean) {
  const scope = getSettlementScope(getHostCurrentUser());
  const store = useMemo(
    () =>
      createSettlementCenterStore(scope, {
        cards: async () => (await settlementApi.listWorkspaceCards()).cards,
        proposals: async () => (await skillProposalApi.listWorkspace()).skill_proposals,
      }),
    [scope],
  );
  const lists = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const previousActive = useRef(active);
  const [visit, setVisit] = useState(() => ({
    scope,
    value: readSettlementVisit(scope, visitStorage()),
    active,
  }));

  useEffect(() => {
    setVisit({
      scope,
      value: active
        ? markSettlementVisited(scope, visitStorage())
        : readSettlementVisit(scope, visitStorage()),
      active,
    });
  }, [scope, active]);

  useEffect(() => {
    void store.refresh();
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void store.refresh(true);
    };
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      store.invalidate();
    };
  }, [store]);

  useEffect(() => {
    if (active && !previousActive.current) void store.refresh();
    previousActive.current = active;
  }, [active, store]);

  const actions = useMemo(
    () => ({
      confirmCard: (card: SettlementCard, fields: Record<string, string>) =>
        store.runCardAction(
          card,
          async () => (await settlementApi.confirmCard(card.session_id, card.id, fields)).card,
        ),
      dismissCard: (card: SettlementCard) =>
        store.runCardAction(
          card,
          async () => (await settlementApi.dismissCard(card.session_id, card.id)).card,
        ),
      acceptProposal: (proposal: SkillProposal, selection: SkillProposalAcceptSelection = {}) =>
        store.runProposalAction(
          proposal,
          async () =>
            (await skillProposalApi.accept(proposal.session_id, proposal.id, selection))
              .skill_proposal,
        ),
      rejectProposal: (proposal: SkillProposal) =>
        store.runProposalAction(
          proposal,
          async () =>
            (await skillProposalApi.reject(proposal.session_id, proposal.id)).skill_proposal,
        ),
    }),
    [store],
  );

  const lastVisit =
    visit.scope === scope ? visit.value : readSettlementVisit(scope, visitStorage());
  const unreadCount =
    !scope || (active && (!visit.active || visit.scope !== scope))
      ? 0
      : countUnreadSettlementCards(lists.cards.items, lastVisit);
  return {
    scope,
    ...lists,
    ...actions,
    unreadCount,
    refreshCards: store.refreshCards,
    refreshProposals: store.refreshProposals,
  };
}

export type SettlementCenterController = ReturnType<typeof useSettlementCenter>;
