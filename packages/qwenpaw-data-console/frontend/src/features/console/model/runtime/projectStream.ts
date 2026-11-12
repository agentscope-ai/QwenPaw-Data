import type {
  BizEvent,
  ConsoleChat,
  ConsoleStreamEvent,
  ContextUsage,
  MessageContent,
  ResponseStatus,
  Segment,
  TraceMessage,
} from '../../api/types';
import { applyPlanToolOutput } from '../plan/planProjection.js';
import { createMcpFailureMessage } from '../chat/mcpFailureMessage.js';
import { readContextUsage } from './contextUsage.js';
import { mergeTimingEvents, toTimingEvent } from './executionTiming.js';
import type { TimingEvent } from '../../api/types.js';

export interface ChatStreamProjection {
  timingEvents?: TimingEvent[];
  chat: ConsoleChat;
  responseId: string | null;
  responseStatus: ResponseStatus | null;
  messages: TraceMessage[];
  lastSequenceNumber: number;
  streamError: { code: string; message: string } | null;
  contextUsage?: ContextUsage | null;
}

function upsertByKey<T>(items: T[], next: T, getKey: (item: T) => string): T[] {
  const nextKey = getKey(next);
  const index = items.findIndex((item) => getKey(item) === nextKey);
  if (index < 0) return [...items, next];
  return items.map((item, itemIndex) => (itemIndex === index ? next : item));
}

function upsertById<T extends { id: string }>(items: T[], next: T): T[] {
  return upsertByKey(items, next, (item) => item.id);
}

function upsertBizEvent(items: BizEvent[], next: BizEvent): BizEvent[] {
  const current = items.find((item) => item.event_id === next.event_id);
  const stableEvent = current ? { ...next, seq: current.seq } : next;
  return upsertByKey(items, stableEvent, (item) => item.event_id).sort(
    (left, right) => left.seq - right.seq,
  );
}

function upsertSegment(items: Segment[], next: Segment): Segment[] {
  return upsertByKey(items, next, (item) => item.segment_id).sort(
    (left, right) =>
      left.coverage.start_seq - right.coverage.start_seq ||
      left.coverage.end_seq - right.coverage.end_seq,
  );
}

const APPENDABLE_DATA_FIELDS = new Set(['arguments', 'content', 'markdown', 'output', 'text']);

function mergeDataDelta(current: unknown, delta: unknown): unknown {
  if (delta === null || delta === undefined) return current;
  if (current === null || current === undefined) return delta;
  if (typeof current === 'string' && typeof delta === 'string') {
    return `${current}${delta}`;
  }
  if (Array.isArray(current) && Array.isArray(delta)) {
    return [...current, ...delta];
  }
  if (
    typeof current === 'object' &&
    !Array.isArray(current) &&
    typeof delta === 'object' &&
    !Array.isArray(delta)
  ) {
    const currentRecord = current as Record<string, unknown>;
    const deltaRecord = delta as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries({ ...currentRecord, ...deltaRecord }).map(([key, value]) => {
        const currentValue = currentRecord[key];
        if (
          APPENDABLE_DATA_FIELDS.has(key) &&
          typeof currentValue === 'string' &&
          typeof deltaRecord[key] === 'string'
        ) {
          return [key, `${currentValue}${deltaRecord[key]}`];
        }
        return [key, value];
      }),
    );
  }
  return delta;
}

function mergeContent(message: TraceMessage, event: MessageContent): TraceMessage {
  const index = event.index ?? message.content.length;
  const current = message.content[index];
  const next =
    event.delta && current
      ? {
          ...current,
          ...event,
          text: `${current.text ?? ''}${event.text ?? ''}`,
          refusal: `${current.refusal ?? ''}${event.refusal ?? ''}` || null,
          data: mergeDataDelta(current.data, event.data),
        }
      : event;
  const content = [...message.content];
  content[index] = next;
  return { ...message, content };
}

export function createChatStreamProjection(
  chat: ConsoleChat,
  messages: TraceMessage[] = [],
): ChatStreamProjection {
  const normalizedMessageById = new Map<string, TraceMessage>();
  for (const message of messages) {
    normalizedMessageById.set(message.id, message);
  }
  const normalizedMessages = [...normalizedMessageById.values()].sort(
    (left, right) => left.sequence - right.sequence,
  );
  const planCallMessagesById = new Map<string, TraceMessage>();
  for (const message of normalizedMessages) {
    if (message.type === 'plugin_call' && message.source_id) {
      planCallMessagesById.set(message.source_id, message);
    }
  }
  const replayedPlan = normalizedMessages.reduce(
    (plan, message) => applyPlanToolOutput(plan, normalizedMessages, message, planCallMessagesById),
    chat.plan,
  );
  const normalizedChat = {
    ...chat,
    plan: replayedPlan,
    biz_events: chat.biz_events
      .filter((event) => event.channel === 'main')
      .sort((left, right) => left.seq - right.seq),
    segments: [...chat.segments].sort(
      (left, right) =>
        left.coverage.start_seq - right.coverage.start_seq ||
        left.coverage.end_seq - right.coverage.end_seq,
    ),
  };

  return {
    chat: normalizedChat,
    responseId: null,
    responseStatus: null,
    messages: normalizedMessages,
    lastSequenceNumber: chat.last_sequence_number,
    streamError: null,
    contextUsage: null,
  };
}

export function projectStreamEvent(
  state: ChatStreamProjection,
  event: ConsoleStreamEvent,
): ChatStreamProjection {
  if (
    event.chat_id !== state.chat.id ||
    event.session_id !== state.chat.session_id ||
    event.sequence_number <= state.lastSequenceNumber
  ) {
    return state;
  }

  const timingEvent = toTimingEvent(event);
  const previousTimingEvents = state.timingEvents ?? [];
  const lastTimingEvent = previousTimingEvents[previousTimingEvents.length - 1];
  const base = {
    ...state,
    timingEvents: !timingEvent
      ? state.timingEvents
      : !lastTimingEvent || lastTimingEvent.sequence_number < timingEvent.sequence_number
        ? [...previousTimingEvents, timingEvent]
        : mergeTimingEvents([timingEvent], previousTimingEvents, state.chat),
    lastSequenceNumber: event.sequence_number,
    chat: {
      ...state.chat,
      last_sequence_number: event.sequence_number,
    },
  };

  switch (event.object) {
    case 'mcp.connect': {
      const message = createMcpFailureMessage(event);
      return message ? { ...base, messages: upsertById(base.messages, message) } : base;
    }
    case 'context.usage':
      return {
        ...base,
        contextUsage: readContextUsage(event) ?? state.contextUsage,
      };
    case 'response':
      return {
        ...base,
        chat: {
          ...base.chat,
          status:
            event.status === 'completed'
              ? 'completed'
              : event.status === 'failed'
                ? 'failed'
                : event.status === 'cancelled'
                  ? 'canceled'
                  : 'running',
          forkable: event.status === 'completed',
          completed_at: ['completed', 'failed', 'cancelled'].includes(event.status)
            ? base.chat.completed_at
            : null,
          error: event.error
            ? { code: event.error.code, message: event.error.message }
            : base.chat.error,
        },
        responseId: event.id,
        responseStatus: event.status,
        streamError: event.error
          ? { code: event.error.code, message: event.error.message }
          : base.streamError,
      };
    case 'message': {
      const messages = upsertById(base.messages, event);
      const plan =
        event.type === 'plugin_call_output'
          ? applyPlanToolOutput(base.chat.plan, messages, event)
          : base.chat.plan;
      return {
        ...base,
        messages,
        chat: { ...base.chat, plan },
      };
    }
    case 'content':
      return {
        ...base,
        messages: base.messages.map((message) =>
          message.id === event.msg_id ? mergeContent(message, event) : message,
        ),
      };
    case 'segment':
      return {
        ...base,
        chat: {
          ...base.chat,
          segments: upsertSegment(base.chat.segments, event.segment),
        },
      };
    case 'biz_event':
      return {
        ...base,
        chat: {
          ...base.chat,
          biz_events:
            event.biz_event.channel === 'main'
              ? upsertBizEvent(base.chat.biz_events, event.biz_event)
              : base.chat.biz_events,
        },
      };
    case 'artifact.registered':
      return {
        ...base,
        chat: {
          ...base.chat,
          artifacts: upsertById(base.chat.artifacts, event.artifact),
        },
      };
    case 'followup.generated':
      return {
        ...base,
        chat: { ...base.chat, followup: event.followup },
      };
    case 'error':
      return {
        ...base,
        streamError: { code: event.code, message: event.message },
      };
  }

  return base;
}
