import type { BizEvent, Segment, TraceMessage } from '../api/types';
import type { ChatStreamProjection } from './projectStream';

export type ChatTimelineItem =
  | {
      kind: 'user';
      id: string;
      seq: number;
      text: string;
      message: TraceMessage | null;
    }
  | {
      kind: 'raw';
      id: string;
      seq: number;
      message: TraceMessage;
    }
  | {
      kind: 'biz_event';
      id: string;
      seq: number;
      event: BizEvent;
    }
  | {
      kind: 'segment';
      id: string;
      seq: number;
      segment: Segment;
    };

export type ChatTimelineSection =
  | {
      kind: 'response';
      id: string;
      items: ChatTimelineItem[];
    }
  | {
      kind: 'steer';
      id: string;
      text: string;
    };

export interface SelectChatTimelineOptions {
  keepCoveredBizEvents?: boolean;
}

function isCoveredBySegment(seq: number, segments: Segment[]): boolean {
  return segments.some(({ coverage }) => (
    seq >= coverage.start_seq && seq <= coverage.end_seq
  ));
}

function itemPriority(item: ChatTimelineItem): number {
  if (item.kind === 'user') return 0;
  if (item.kind === 'raw') return 1;
  if (item.kind === 'biz_event') return 2;
  return 3;
}

export function mergeToolBizEvents(events: BizEvent[]): BizEvent[] {
  const merged: BizEvent[] = [];
  const toolIndexByBlockId = new Map<string, number>();

  [...events]
    .sort((left, right) => left.seq - right.seq)
    .forEach((event) => {
      const blockId = event.block_id;
      if (!blockId || event.presentation?.card_type !== 'tool') {
        merged.push(event);
        return;
      }

      const existingIndex = toolIndexByBlockId.get(blockId);
      if (existingIndex === undefined) {
        toolIndexByBlockId.set(blockId, merged.length);
        merged.push(event);
        return;
      }

      const existing = merged[existingIndex];
      const existingBody = existing.presentation?.body?.trim() ?? '';
      const nextBody = event.presentation.body?.trim() ?? '';
      const body = existingBody && nextBody && existingBody !== nextBody
        ? `${existingBody}\n\n${nextBody}`
        : nextBody || existingBody;
      merged[existingIndex] = {
        ...existing,
        status: existing.status === 'error' || event.status === 'error' ? 'error' : 'done',
        presentation: {
          card_type: 'tool',
          caption: event.presentation.caption || existing.presentation?.caption || '工具调用',
          body,
        },
        ended_at: event.ended_at ?? existing.ended_at,
      };
    });

  return merged;
}

export function selectChatTimeline(
  projection: ChatStreamProjection,
  options: SelectChatTimelineOptions = {},
): ChatTimelineItem[] {
  const steerTraceMessages = projection.messages.filter((message) => (
    message.role === 'user' && message.metadata?.source === 'steer'
  ));
  const steerMessages = steerTraceMessages
    .map<ChatTimelineItem>((message) => ({
      kind: 'user',
      id: message.id,
      seq: message.sequence,
      text: message.content.map((content) => content.text ?? '').join(''),
      message,
    }));

  const userMessages: ChatTimelineItem[] = [];
  if (projection.chat.user_input) {
    userMessages.push({
      kind: 'user',
      id: `user-${projection.chat.id}`,
      seq: 0,
      text: projection.chat.user_input,
      message: null,
    });
  }
  userMessages.push(...steerMessages);

  const steerMessageIds = new Set(steerMessages.map((item) => item.id));
  const steerSourceIds = new Set(
    steerTraceMessages
      .map((message) => message.source_id)
      .filter((sourceId): sourceId is string => Boolean(sourceId)),
  );
  const rawMessages = projection.messages.filter(
    (message) => !steerMessageIds.has(message.id),
  );
  const rawIdsBySourceId = new Map<string, Set<string>>();
  rawMessages.forEach((message) => {
    if (message.source_id) {
      const ids = rawIdsBySourceId.get(message.source_id) ?? new Set<string>();
      ids.add(message.id);
      rawIdsBySourceId.set(message.source_id, ids);
    }
  });

  const replacedRawIds = new Set<string>();
  const presentableBizEvents = mergeToolBizEvents(projection.chat.biz_events).filter((event) => {
    if (event.channel !== 'main' || !event.presentation) return false;
    // A successful steer is represented by its user bubble. The matching
    // business projection is useful for trace diagnostics but would duplicate
    // the same text in the conversation.
    if (event.block_id && steerSourceIds.has(event.block_id)) return false;
    if (event.block_id) {
      rawIdsBySourceId.get(event.block_id)
        ?.forEach((messageId) => replacedRawIds.add(messageId));
    }
    return true;
  });

  const rawItems = rawMessages
    .filter((message) => !replacedRawIds.has(message.id))
    .map<ChatTimelineItem>((message) => ({
      kind: 'raw',
      id: message.id,
      seq: message.sequence,
      message,
    }));
  const segments = [...projection.chat.segments].sort((left, right) => (
    left.coverage.start_seq - right.coverage.start_seq
    || left.coverage.end_seq - right.coverage.end_seq
  ));
  const bizEventItems = presentableBizEvents
    .filter((event) => (
      options.keepCoveredBizEvents || !isCoveredBySegment(event.seq, segments)
    ))
    .map<ChatTimelineItem>((event) => ({
      kind: 'biz_event',
      id: event.event_id,
      seq: event.seq,
      event,
    }));
  const segmentItems = segments.map<ChatTimelineItem>((segment) => ({
    kind: 'segment',
    id: segment.segment_id,
    seq: segment.coverage.start_seq,
    segment,
  }));

  return [...userMessages, ...rawItems, ...bizEventItems, ...segmentItems]
    .sort((left, right) => (
      left.seq - right.seq
      || itemPriority(left) - itemPriority(right)
      || left.id.localeCompare(right.id)
    ));
}

export function splitChatTimelineBySteer(
  timeline: ChatTimelineItem[],
): ChatTimelineSection[] {
  const sections: ChatTimelineSection[] = [];
  let responseItems: ChatTimelineItem[] = [];

  const flushResponse = () => {
    if (!responseItems.length) return;
    sections.push({
      kind: 'response',
      id: `response-${responseItems[0].id}`,
      items: responseItems,
    });
    responseItems = [];
  };

  timeline.forEach((item) => {
    if (item.kind === 'user') {
      if (item.message?.metadata?.source === 'steer') {
        flushResponse();
        sections.push({ kind: 'steer', id: item.id, text: item.text });
      }
      return;
    }
    responseItems.push(item);
  });
  flushResponse();

  return sections;
}
