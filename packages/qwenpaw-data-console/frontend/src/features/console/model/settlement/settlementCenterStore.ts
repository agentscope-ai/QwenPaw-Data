import type { SettlementCard } from '../../api/settlement';
import type { SkillProposal } from '../../api/skillProposals';
import { settlementItemKey } from './settlementCenter.js';

export interface SettlementListState<T> {
  items: T[];
  loading: boolean;
  loaded: boolean;
  error: Error | null;
}

interface ListRequests {
  cards: () => Promise<SettlementCard[]>;
  proposals: () => Promise<SkillProposal[]>;
}

function initialList<T>(enabled: boolean): SettlementListState<T> {
  return { items: [], loading: enabled, loaded: false, error: null };
}

/** One store per authenticated scope; invalidation discards all late responses. */
export function createSettlementCenterStore(scope: string | null, requests: ListRequests) {
  let snapshot = {
    cards: initialList<SettlementCard>(Boolean(scope)),
    proposals: initialList<SkillProposal>(Boolean(scope)),
    busy: {} as Record<string, boolean>,
  };
  const listeners = new Set<() => void>();
  const generations = { cards: 0, proposals: 0 };
  let epoch = 0;
  const notify = () => listeners.forEach((listener) => listener());

  async function load(kind: keyof ListRequests, background = false) {
    if (!scope || Object.keys(snapshot.busy).some((key) => key.startsWith(`${kind}:`))) return;
    const generation = ++generations[kind];
    if (!background || !snapshot[kind].loaded) {
      snapshot = { ...snapshot, [kind]: { ...snapshot[kind], loading: true, error: null } };
      notify();
    }
    try {
      const items = await requests[kind]();
      if (generation !== generations[kind]) return;
      const current = snapshot[kind];
      if (
        current.loaded &&
        !current.loading &&
        !current.error &&
        JSON.stringify(current.items) === JSON.stringify(items)
      )
        return;
      snapshot = { ...snapshot, [kind]: { items, loaded: true, loading: false, error: null } };
      notify();
    } catch (error) {
      if (generation !== generations[kind]) return;
      snapshot = {
        ...snapshot,
        [kind]: {
          ...snapshot[kind],
          loading: false,
          error: error instanceof Error ? error : new Error(String(error)),
        },
      };
      notify();
    }
  }

  async function runAction<T extends SettlementCard | SkillProposal>(
    kind: keyof ListRequests,
    item: T,
    work: () => Promise<T>,
  ): Promise<T | undefined> {
    if (!scope) throw new Error('SETTLEMENT_SCOPE_UNAVAILABLE');
    if (!item.session_id?.trim()) throw new Error('SETTLEMENT_SESSION_UNAVAILABLE');
    const key = `${kind}:${settlementItemKey(item)}`;
    if (snapshot.busy[key]) return undefined;
    const actionEpoch = epoch;
    generations[kind] += 1;
    snapshot = {
      ...snapshot,
      busy: { ...snapshot.busy, [key]: true },
      [kind]: { ...snapshot[kind], loading: false },
    };
    notify();
    let succeeded = false;
    try {
      const result = await work();
      if (actionEpoch !== epoch) return undefined;
      generations[kind] += 1;
      const items = snapshot[kind].items.filter(
        (existing) => settlementItemKey(existing) !== settlementItemKey(item),
      );
      snapshot = { ...snapshot, [kind]: { ...snapshot[kind], items: [...items, result] } };
      succeeded = true;
      notify();
      return result;
    } catch (error) {
      if (actionEpoch !== epoch) return undefined;
      throw error;
    } finally {
      if (actionEpoch === epoch) {
        const busy = { ...snapshot.busy };
        delete busy[key];
        snapshot = { ...snapshot, busy };
        notify();
        if (succeeded) void load(kind);
      }
    }
  }

  return {
    scope,
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    refreshCards: () => load('cards'),
    refreshProposals: () => load('proposals'),
    refresh: (background = false) =>
      Promise.all([load('cards', background), load('proposals', background)]),
    runCardAction: (item: SettlementCard, work: () => Promise<SettlementCard>) =>
      runAction('cards', item, work),
    runProposalAction: (item: SkillProposal, work: () => Promise<SkillProposal>) =>
      runAction('proposals', item, work),
    invalidate() {
      epoch += 1;
      generations.cards += 1;
      generations.proposals += 1;
      snapshot = { ...snapshot, busy: {} };
    },
  };
}
