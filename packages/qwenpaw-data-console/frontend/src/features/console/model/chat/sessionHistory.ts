import type { ConsoleChat, SessionSnapshot, TraceMessage, TimingEvent } from '../../api/types.js';
import { mergeTimingEvents } from '../runtime/executionTiming.js';
import { createChatStreamProjection, type ChatStreamProjection } from '../runtime/projectStream.js';

export function mergeHistoryPage(current: ChatStreamProjection[], chats: ConsoleChat[]) {
  const known = new Set(current.map(({ chat }) => chat.id));
  return [
    ...current,
    ...chats.filter((chat) => !known.has(chat.id)).map((chat) => createChatStreamProjection(chat)),
  ].sort((a, b) => a.chat.sequence - b.chat.sequence);
}

export function historyCursor(snapshot: SessionSnapshot): number | null {
  if (!snapshot.has_more || !snapshot.chats.length) return null;
  return Math.min(...snapshot.chats.map((chat) => chat.sequence));
}

// Trace is a delayed HTTP snapshot. Live messages, status and replay cursor win.
export function hydrateProjectionTrace(
  projection: ChatStreamProjection,
  trace: TraceMessage[],
  timingEvents: TimingEvent[] = [],
) {
  if (timingEvents.length) {
    projection = {
      ...projection,
      timingEvents: mergeTimingEvents(projection.timingEvents ?? [], timingEvents, projection.chat),
    };
  }
  const known = new Set(projection.messages.map((message) => message.id));
  if (
    trace.every(
      (message) =>
        (message.chat_id && message.chat_id !== projection.chat.id) || known.has(message.id),
    )
  ) {
    return projection;
  }
  const messages = new Map(
    trace
      .filter((message) => !message.chat_id || message.chat_id === projection.chat.id)
      .map((message) => [message.id, message]),
  );
  projection.messages.forEach((message) => messages.set(message.id, message));
  const hydrated = createChatStreamProjection(projection.chat, [...messages.values()]);
  return { ...projection, messages: hydrated.messages };
}

// Bound raw trace requests, and publish each result without waiting for its peers.
export async function loadHistoryTraces(
  chats: ConsoleChat[],
  signal: AbortSignal,
  load: (chat: ConsoleChat) => Promise<TraceMessage[]>,
  publish: (chat: ConsoleChat, messages: TraceMessage[]) => void,
  failed: (chat: ConsoleChat) => void,
) {
  let index = 0;
  await Promise.all(
    Array.from({ length: Math.min(4, chats.length) }, async () => {
      while (!signal.aborted && index < chats.length) {
        const chat = chats[index++];
        try {
          const messages = await load(chat);
          if (!signal.aborted) publish(chat, messages);
        } catch {
          if (!signal.aborted) failed(chat);
        }
      }
    }),
  );
}

/** One queue per session load: dedupe visible/selected requests and cap total concurrency. */
export function createHistoryTraceQueue<T = TraceMessage[]>(
  signal: AbortSignal,
  load: (chat: ConsoleChat) => Promise<T>,
  publish: (chat: ConsoleChat, messages: T) => void,
  failed: (chat: ConsoleChat) => void,
) {
  const known = new Set<string>();
  const pending: ConsoleChat[] = [];
  let active = 0;
  const drain = () => {
    while (!signal.aborted && active < 2 && pending.length) {
      const chat = pending.shift()!;
      active++;
      void Promise.resolve()
        .then(() => {
          signal.throwIfAborted();
          return load(chat);
        })
        .then(
          (messages) => {
            if (!signal.aborted) publish(chat, messages);
          },
          () => {
            known.delete(chat.id);
            if (!signal.aborted) failed(chat);
          },
        )
        .finally(() => {
          active--;
          drain();
        });
    }
  };
  signal.addEventListener(
    'abort',
    () => {
      pending.length = 0;
      known.clear();
    },
    { once: true },
  );
  return (chats: ConsoleChat[]) => {
    if (signal.aborted) return;
    for (const chat of chats) {
      if (known.has(chat.id)) continue;
      known.add(chat.id);
      pending.push(chat);
    }
    drain();
  };
}
