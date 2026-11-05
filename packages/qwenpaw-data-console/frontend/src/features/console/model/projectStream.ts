import type {
  BizEvent,
  ConsoleChat,
  ConsoleStreamEvent,
  MessageContent,
  ResponseStatus,
  Segment,
  TraceMessage,
} from '../api/types';
import { applyPlanToolOutput, replayPlanFromMessages } from './planProjection.js';

export interface ChatStreamProjection {
  chat: ConsoleChat;
  responseId: string | null;
  responseStatus: ResponseStatus | null;
  messages: TraceMessage[];
  lastSequenceNumber: number;
  streamError: { code: string; message: string } | null;
}

function upsertByKey<T>(
  items: T[],
  next: T,
  getKey: (item: T) => string,
): T[] {
  const nextKey = getKey(next);
  const index = items.findIndex((item) => getKey(item) === nextKey);
  if (index < 0) return [...items, next];
  return items.map((item, itemIndex) => itemIndex === index ? next : item);
}

function upsertById<T extends { id: string }>(items: T[], next: T): T[] {
  return upsertByKey(items, next, (item) => item.id);
}

function createErrorTraceMessage(
  chatId: string,
  sequence: number,
  error: { code: string; message: string },
): TraceMessage {
  return {
    id: `error-${sequence}`,
    object: 'message',
    chat_id: chatId,
    sequence,
    type: 'error',
    role: 'assistant',
    content: [],
    status: 'failed',
    metadata: { code: error.code, message: error.message },
  };
}

function hasErrorTraceMessage(messages: TraceMessage[]): boolean {
  return messages.some((message) => message.type === 'error');
}

export function withChatErrorMessage(
  messages: TraceMessage[],
  chatId: string,
  sequence: number,
  error: { code: string; message: string } | null,
): TraceMessage[] {
  if (!error || hasErrorTraceMessage(messages)) return messages;
  return upsertById(messages, createErrorTraceMessage(chatId, sequence, error));
}

function upsertBizEvent(items: BizEvent[], next: BizEvent): BizEvent[] {
  const current = items.find((item) => item.event_id === next.event_id);
  const stableEvent = current ? { ...next, seq: current.seq } : next;
  return upsertByKey(items, stableEvent, (item) => item.event_id)
    .sort((left, right) => left.seq - right.seq);
}

function upsertSegment(items: Segment[], next: Segment): Segment[] {
  return upsertByKey(items, next, (item) => item.segment_id)
    .sort((left, right) => (
      left.coverage.start_seq - right.coverage.start_seq
      || left.coverage.end_seq - right.coverage.end_seq
    ));
}

const APPENDABLE_DATA_FIELDS = new Set([
  'arguments',
  'content',
  'markdown',
  'output',
  'text',
]);

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
    typeof current === 'object'
    && !Array.isArray(current)
    && typeof delta === 'object'
    && !Array.isArray(delta)
  ) {
    const currentRecord = current as Record<string, unknown>;
    const deltaRecord = delta as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries({ ...currentRecord, ...deltaRecord }).map(([key, value]) => {
        const currentValue = currentRecord[key];
        if (
          APPENDABLE_DATA_FIELDS.has(key)
          && typeof currentValue === 'string'
          && typeof deltaRecord[key] === 'string'
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
  const next = event.delta && current
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

export function resolveChatPlan(
  chat: Pick<ConsoleChat, 'plan'>,
  messages: TraceMessage[],
): ConsoleChat['plan'] {
  return chat.plan?.tasks.length ? chat.plan : replayPlanFromMessages(messages);
}

export function createChatStreamProjection(
  chat: ConsoleChat,
  messages: TraceMessage[] = [],
): ChatStreamProjection {
  const normalizedMessages = withChatErrorMessage(
    messages.reduce<TraceMessage[]>(
      (items, message) => upsertById(items, message),
      [],
    ),
    chat.id,
    Math.max(chat.last_sequence_number, 0),
    chat.error,
  );
  // Prefer persisted chat.plan; rebuild from Plan* tool history when empty
  // (refresh during a still-running chat — backend only writes plan at _finish).
  const plan = resolveChatPlan(chat, normalizedMessages);
  const normalizedChat = {
    ...chat,
    plan,
    biz_events: chat.biz_events
      .filter((event) => event.channel === 'main')
      .sort((left, right) => left.seq - right.seq),
    segments: [...chat.segments].sort((left, right) => (
      left.coverage.start_seq - right.coverage.start_seq
      || left.coverage.end_seq - right.coverage.end_seq
    )),
  };

  return {
    chat: normalizedChat,
    responseId: null,
    responseStatus: null,
    messages: normalizedMessages,
    lastSequenceNumber: chat.last_sequence_number,
    streamError: null,
  };
}

export function projectStreamEvent(
  state: ChatStreamProjection,
  event: ConsoleStreamEvent,
): ChatStreamProjection {
  if (
    event.chat_id !== state.chat.id
    || event.sequence_number <= state.lastSequenceNumber
  ) {
    return state;
  }

  const base = {
    ...state,
    lastSequenceNumber: event.sequence_number,
    chat: {
      ...state.chat,
      last_sequence_number: event.sequence_number,
    },
  };

  switch (event.object) {
    case 'response': {
      const error = event.error
        ? { code: event.error.code, message: event.error.message }
        : null;
      return {
        ...base,
        messages: withChatErrorMessage(
          base.messages,
          base.chat.id,
          event.sequence_number,
          error,
        ),
        chat: {
          ...base.chat,
          status: event.status === 'completed'
            ? 'completed'
            : event.status === 'failed'
              ? 'failed'
              : event.status === 'cancelled'
                ? 'canceled'
                : 'running',
          completed_at: ['completed', 'failed', 'cancelled'].includes(event.status)
            ? base.chat.completed_at
            : null,
          error: error ?? base.chat.error,
        },
        responseId: event.id,
        responseStatus: event.status,
        streamError: error ?? base.streamError,
      };
    }
    case 'message': {
      const messages = upsertById(base.messages, event);
      const plan = event.type === 'plugin_call_output'
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
        messages: base.messages.map((message) => (
          message.id === event.msg_id ? mergeContent(message, event) : message
        )),
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
          biz_events: event.biz_event.channel === 'main'
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
    case 'error': {
      const error = { code: event.code, message: event.message };
      return {
        ...base,
        streamError: error,
        messages: upsertById(
          base.messages,
          createErrorTraceMessage(base.chat.id, event.sequence_number, error),
        ),
        chat: { ...base.chat, error },
      };
    }
  }

  return base;
}
