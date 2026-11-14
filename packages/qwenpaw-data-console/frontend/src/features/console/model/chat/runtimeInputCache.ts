import { shouldClearAcceptedRuntimeInput } from '../runtime/runtimeRequest.js';
import type { AttachmentRef } from '../../api/types.js';

interface RuntimeInputCacheEntry {
  attachmentNames: Set<string>;
  draft: string;
  attachments: readonly AttachmentRef[];
}

const runtimeInputCache = new Map<string, RuntimeInputCacheEntry>();
const EMPTY_ATTACHMENTS: readonly AttachmentRef[] = [];
const attachmentListeners = new Set<() => void>();

export function subscribeRuntimeInputAttachments(listener: () => void) {
  attachmentListeners.add(listener);
  return () => {
    attachmentListeners.delete(listener);
  };
}

export function getRuntimeInputAttachments(sessionId: string): readonly AttachmentRef[] {
  return readEntry(sessionId)?.attachments ?? EMPTY_ATTACHMENTS;
}

export function cacheRuntimeInputAttachments(
  sessionId: string,
  attachments: readonly AttachmentRef[],
) {
  if (!sessionId || !attachments.length) return;
  const entry = ensureEntry(sessionId);
  const merged = new Map(entry.attachments.map((item) => [item.attachment_id, item]));
  attachments.forEach((item) => merged.set(item.attachment_id, item));
  entry.attachments = [...merged.values()];
  attachments.forEach((item) => entry.attachmentNames.add(item.filename));
  attachmentListeners.forEach((listener) => listener());
}

export function consumeRuntimeInputAttachments(sessionId: string, ids: readonly string[]) {
  const entry = readEntry(sessionId);
  if (!entry || !entry.attachments.some((item) => ids.includes(item.attachment_id))) return;
  entry.attachments = entry.attachments.filter((item) => !ids.includes(item.attachment_id));
  attachmentListeners.forEach((listener) => listener());
  pruneEntry(sessionId);
}

function readEntry(sessionId: string): RuntimeInputCacheEntry | undefined {
  return sessionId ? runtimeInputCache.get(sessionId) : undefined;
}

function ensureEntry(sessionId: string): RuntimeInputCacheEntry {
  const existing = readEntry(sessionId);
  if (existing) return existing;

  const entry: RuntimeInputCacheEntry = {
    attachmentNames: new Set(),
    draft: '',
    attachments: EMPTY_ATTACHMENTS,
  };
  runtimeInputCache.set(sessionId, entry);
  return entry;
}

function pruneEntry(sessionId: string) {
  const entry = readEntry(sessionId);
  if (entry && !entry.draft && !entry.attachmentNames.size && !entry.attachments.length) {
    runtimeInputCache.delete(sessionId);
  }
}

export function getRuntimeInputDraft(sessionId: string): string {
  return readEntry(sessionId)?.draft ?? '';
}

export function cacheRuntimeInputDraft(sessionId: string, draft: string) {
  if (!sessionId) return;
  if (!draft) {
    const entry = readEntry(sessionId);
    if (entry) entry.draft = '';
    pruneEntry(sessionId);
    return;
  }
  ensureEntry(sessionId).draft = draft;
}

export function clearRuntimeInputDraft(sessionId: string) {
  cacheRuntimeInputDraft(sessionId, '');
}

export function clearAcceptedRuntimeInputDraft(sessionId: string, submittedText: string) {
  if (shouldClearAcceptedRuntimeInput(getRuntimeInputDraft(sessionId), submittedText)) {
    clearRuntimeInputDraft(sessionId);
  }
}

export function getRuntimeInputAttachmentNames(sessionId: string): readonly string[] {
  return [...(readEntry(sessionId)?.attachmentNames ?? [])];
}

export function rememberRuntimeInputAttachmentName(sessionId: string, filename: string) {
  if (!sessionId || !filename) return;
  ensureEntry(sessionId).attachmentNames.add(filename);
}

export function moveRuntimeInputCache(sourceSessionId: string, targetSessionId: string) {
  if (!sourceSessionId || !targetSessionId || sourceSessionId === targetSessionId) return;
  const source = readEntry(sourceSessionId);
  if (!source) return;

  const target = ensureEntry(targetSessionId);
  if (!target.draft) target.draft = source.draft;
  source.attachmentNames.forEach((filename) => target.attachmentNames.add(filename));
  cacheRuntimeInputAttachments(targetSessionId, source.attachments);
  runtimeInputCache.delete(sourceSessionId);
}

export function resetRuntimeInputCacheForTest() {
  runtimeInputCache.clear();
}
