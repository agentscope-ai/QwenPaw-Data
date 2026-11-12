import type { SettlementCard } from '../../api/settlement';
import type { SkillProposal } from '../../api/skillProposals';
import type { HostCurrentUser } from '@/services/auth';
import { getSettlementCardTitle } from './settlementCard.js';

export type SettlementView = 'pending' | 'processed';

export function settlementItemKey(item: { session_id: string; id: string }): string {
  return JSON.stringify([item.session_id, item.id]);
}

export function getSettlementScope(user: HostCurrentUser | null): string | null {
  if (!user?.user_id?.trim() || !user.tenant_id?.trim() || !user.active_workspace_id?.trim()) {
    return null;
  }
  return JSON.stringify([user.user_id, user.tenant_id, user.active_workspace_id]);
}

export function isPendingCard(card: SettlementCard): boolean {
  return card.status === 'pending' || card.status === 'queried';
}

export function settlementTimestamp(
  item: { created_at?: string | null; updated_at?: string | null },
  view: SettlementView,
): number | null {
  for (const value of view === 'processed'
    ? [item.updated_at, item.created_at]
    : [item.created_at]) {
    if (!value?.trim()) continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return null;
}

function newestFirst<T extends { created_at?: string | null; updated_at?: string | null }>(
  items: T[],
  view: SettlementView,
): T[] {
  return [...items].sort(
    (left, right) =>
      (settlementTimestamp(right, view) ?? -Infinity) -
      (settlementTimestamp(left, view) ?? -Infinity),
  );
}

export function selectSettlementItems(
  cards: SettlementCard[],
  proposals: SkillProposal[],
  view: SettlementView,
  searchText: string,
) {
  const query = searchText.trim().toLocaleLowerCase();
  const matches = (values: unknown[]) =>
    !query ||
    values.some((value) =>
      String(value ?? '')
        .toLocaleLowerCase()
        .includes(query),
    );
  return {
    cards: newestFirst(
      cards.filter(
        (card) =>
          (view === 'pending'
            ? isPendingCard(card)
            : ['confirmed', 'dismissed'].includes(card.status)) &&
          matches([getSettlementCardTitle(card), ...Object.values(card.fields ?? {})]),
      ),
      view,
    ),
    proposals: newestFirst(
      proposals.filter(
        (proposal) =>
          (view === 'pending'
            ? proposal.status === 'pending'
            : ['accepted', 'partial', 'rejected'].includes(proposal.status)) &&
          matches([proposal.summary, ...proposal.mentioned_skill_names]),
      ),
      view,
    ),
  };
}
