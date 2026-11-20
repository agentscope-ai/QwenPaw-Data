import { ApiRequestError } from '../../../../services/request.js';
import type { ToolConfirmationDecision } from '../../api/types.js';

export interface ConfirmationTarget {
  sessionId: string;
  chatId: string;
  callId: string;
}
export interface ConfirmationSubmission extends ConfirmationTarget {
  decision: ToolConfirmationDecision;
  status: 'submitting' | 'accepted' | 'unknown' | 'blocked' | 'retry';
  cleanupAt: number;
}
type StoragePort = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const RETENTION = 7 * 24 * 60 * 60 * 1000;
const MAX_RECORDS = 256;

export function confirmationKey(target: ConfirmationTarget) {
  return JSON.stringify([target.sessionId, target.chatId, target.callId]);
}

export function confirmationIdentity(
  host: string,
  user: {
    user_id: string;
    tenant_id: string | null;
    active_workspace_id: string;
  } | null,
) {
  return user?.user_id && user.active_workspace_id
    ? JSON.stringify([host, user.user_id, user.tenant_id, user.active_workspace_id])
    : null;
}

export function classifyConfirmationError(error: unknown): 'retry' | 'blocked' | 'unknown' {
  if (error instanceof ApiRequestError) {
    if ([401, 403, 404, 409].includes(error.status)) return 'blocked';
    if ([400, 422].includes(error.status)) return 'retry';
  }
  return 'unknown';
}

/** One identity per store. Navigation preserves claims; identity changes never reuse them. */
export function createToolConfirmationSubmissions(
  identity: string | null,
  getStorage: () => StoragePort = () => window.sessionStorage,
  now: () => number = Date.now,
) {
  const storageKey = identity ? `qwenpaw-data:tool-confirmation:v1:${identity}` : null;
  let storage: StoragePort | null = null;
  let records: Record<string, ConfirmationSubmission> = {};
  let recoveryLimited = !storageKey;
  const listeners = new Set<() => void>();
  try {
    if (storageKey) {
      storage = getStorage();
      const raw = storage.getItem(storageKey);
      if (raw) {
        const saved: unknown = JSON.parse(raw);
        if (
          !saved ||
          typeof saved !== 'object' ||
          !('version' in saved) ||
          saved.version !== 1 ||
          !('records' in saved) ||
          !Array.isArray(saved.records)
        )
          throw new Error('Invalid recovery data');
        for (const value of saved.records.slice(0, MAX_RECORDS)) {
          if (!value || typeof value !== 'object') throw new Error('Invalid recovery entry');
          const entry = value as ConfirmationSubmission;
          if (
            ![entry.sessionId, entry.chatId, entry.callId].every(
              (id) => typeof id === 'string' && id.trim(),
            ) ||
            !['approved', 'denied'].includes(entry.decision) ||
            !['submitting', 'accepted', 'unknown', 'blocked'].includes(entry.status) ||
            !Number.isFinite(entry.cleanupAt)
          )
            throw new Error('Invalid recovery entry');
          if (entry.cleanupAt <= now()) continue;
          // Select only the allowed fields; tool arguments and credentials never enter storage.
          const restored: ConfirmationSubmission = {
            sessionId: entry.sessionId,
            chatId: entry.chatId,
            callId: entry.callId,
            decision: entry.decision,
            cleanupAt: entry.cleanupAt,
            status: entry.status === 'submitting' ? 'unknown' : entry.status,
          };
          records[confirmationKey(restored)] = restored;
        }
      }
    }
  } catch {
    recoveryLimited = true;
  }
  let snapshot = { records, recoveryLimited };
  const publish = () => {
    snapshot = { records, recoveryLimited };
    listeners.forEach((listener) => listener());
  };
  const persist = () => {
    if (storage && storageKey) {
      try {
        const entries = Object.values(records).filter((record) => record.status !== 'retry');
        if (entries.length > MAX_RECORDS) {
          recoveryLimited = true;
          // Keep all in-memory claims; never evict live entries merely to allow a new submission.
        }
        if (entries.length)
          storage.setItem(
            storageKey,
            JSON.stringify({ version: 1, records: entries.slice(-MAX_RECORDS) }),
          );
        else storage.removeItem(storageKey);
      } catch {
        recoveryLimited = true;
      }
    }
    publish();
  };
  const update = (target: ConfirmationTarget, status: ConfirmationSubmission['status']) => {
    const key = confirmationKey(target);
    // Output/terminal reconciliation may already have removed this claim.
    if (!records[key]) return;
    records = { ...records, [key]: { ...records[key], status } };
    persist();
  };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    reconcile(
      sessionId: string,
      chats: ReadonlyArray<{
        chatId: string;
        terminal: boolean;
        resolvedCallIds: ReadonlySet<string>;
      }>,
    ) {
      let changed = false;
      const next = { ...records };
      for (const [key, record] of Object.entries(next)) {
        const chat =
          record.sessionId === sessionId
            ? chats.find((item) => item.chatId === record.chatId)
            : null;
        if (
          record.cleanupAt <= now() ||
          chat?.terminal ||
          chat?.resolvedCallIds.has(record.callId)
        ) {
          delete next[key];
          changed = true;
        }
      }
      if (changed) {
        records = next;
        persist();
      }
    },
    async submit(
      target: ConfirmationTarget,
      decision: ToolConfirmationDecision,
      access: { assertEligible: () => void; isCurrent: () => boolean },
      send: () => Promise<unknown>,
    ) {
      // A duplicate must not send, even when two clicks occur before React renders.
      const key = confirmationKey(target);
      if (records[key] && records[key].status !== 'retry') return false;
      access.assertEligible();
      records = {
        ...records,
        [key]: { ...target, decision, status: 'submitting', cleanupAt: now() + RETENTION },
      };
      persist();
      try {
        await send();
        update(target, access.isCurrent() ? 'accepted' : 'unknown');
      } catch (error) {
        update(target, access.isCurrent() ? classifyConfirmationError(error) : 'unknown');
      }
      return true;
    },
  };
}

export type ToolConfirmationSubmissions = ReturnType<typeof createToolConfirmationSubmissions>;
