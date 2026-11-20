import type { ConsoleChat } from '../../api/types';

export function segmentInstanceKey(chatId: string, segmentId: string): string {
  return `${chatId}:${segmentId}`;
}

/** Segment IDs are local to a chat; choose the latest segment across the session. */
export function selectLastSessionSegmentKey(chats: readonly ConsoleChat[]): string | null {
  let latest: { chat: ConsoleChat; segment: ConsoleChat['segments'][number] } | null = null;
  for (const chat of chats) {
    for (const segment of chat.segments) {
      if (
        !latest ||
        chat.sequence > latest.chat.sequence ||
        (chat.sequence === latest.chat.sequence &&
          (segment.coverage.start_seq > latest.segment.coverage.start_seq ||
            (segment.coverage.start_seq === latest.segment.coverage.start_seq &&
              segment.coverage.end_seq >= latest.segment.coverage.end_seq)))
      ) {
        latest = { chat, segment };
      }
    }
  }
  return latest ? segmentInstanceKey(latest.chat.id, latest.segment.segment_id) : null;
}
