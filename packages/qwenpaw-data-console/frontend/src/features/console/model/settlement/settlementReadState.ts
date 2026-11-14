import type { SettlementCard } from '../../api/settlement';
import { isPendingCard, settlementTimestamp } from './settlementCenter.js';

type VisitStorage = Pick<Storage, 'getItem' | 'setItem'>;
const memoryVisits = new Map<string, number>();

export function settlementVisitKey(scope: string): string {
  return `qwenpaw-data:settlement:last-visit:v1:${scope}`;
}

export function readSettlementVisit(
  scope: string | null,
  storage: VisitStorage | null,
): number | null {
  if (!scope) return null;
  try {
    const raw = storage?.getItem(settlementVisitKey(scope));
    const value = raw?.trim() ? Number(raw) : NaN;
    if (Number.isFinite(value) && value >= 0) {
      const latest = Math.max(value, memoryVisits.get(scope) ?? 0);
      memoryVisits.set(scope, latest);
      return latest;
    }
  } catch {
    /* Storage may be disabled; keep the current scope's in-memory visit. */
  }
  return memoryVisits.get(scope) ?? null;
}

export function markSettlementVisited(
  scope: string | null,
  storage: VisitStorage | null,
  now = Date.now(),
): number | null {
  if (!scope || !Number.isFinite(now) || now < 0) return null;
  memoryVisits.set(scope, now);
  try {
    storage?.setItem(settlementVisitKey(scope), String(now));
  } catch {
    /* Memory remains available. */
  }
  return now;
}

export function countUnreadSettlementCards(cards: SettlementCard[], visit: number | null): number {
  return cards.filter(
    (card) =>
      isPendingCard(card) &&
      (visit === null || (settlementTimestamp(card, 'pending') ?? -Infinity) > visit),
  ).length;
}
