import type {
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISession,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type {
  IContent,
  IAgentScopeRuntimeMessage,
  IAgentScopeRuntimeRequest,
  IAgentScopeRuntimeResponse,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types.js';
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types.js';
import type {
  AttachmentRef,
  ArtifactComment,
  ConsoleChat,
  ConsoleSession,
  ConsoleStreamEvent,
} from '../../api/types';
import { isTerminalChatStatus } from './chatStreamLifecycle.js';
import type { ChatStreamProjection } from './projectStream.js';
import { selectChatTimeline, type ChatTimelineItem } from './selectChatTimeline.js';
import { createBizEventRuntimeMessage, createSegmentRuntimeMessage } from './runtimeProjection.js';
import { attachmentRefFromRuntimeContent } from './runtimeRequest.js';
import { createRuntimeStreamGuardMessage } from './runtimeSteer.js';

function getRuntimeStatus(chat: ConsoleChat): IAgentScopeRuntimeResponse['status'] {
  if (chat.status === 'completed') return AgentScopeRuntimeRunStatus.Completed;
  if (chat.status === 'failed') return AgentScopeRuntimeRunStatus.Failed;
  if (chat.status === 'canceled') return AgentScopeRuntimeRunStatus.Canceled;
  if (chat.status === 'created') return AgentScopeRuntimeRunStatus.Created;
  return AgentScopeRuntimeRunStatus.InProgress;
}

function getRuntimeResponseStatus(
  status: ChatStreamProjection['responseStatus'],
): AgentScopeRuntimeRunStatus {
  if (status === 'completed') return AgentScopeRuntimeRunStatus.Completed;
  if (status === 'failed') return AgentScopeRuntimeRunStatus.Failed;
  if (status === 'cancelled') return AgentScopeRuntimeRunStatus.Canceled;
  if (status === 'created') return AgentScopeRuntimeRunStatus.Created;
  return AgentScopeRuntimeRunStatus.InProgress;
}

function requestCardContent(chat: ConsoleChat): IContent[] {
  const attachments = chat.attachments ?? [];
  const content: IContent[] = [];
  if (chat.user_input) {
    content.push({
      type: AgentScopeRuntimeContentType.TEXT,
      text: chat.user_input,
      status: AgentScopeRuntimeRunStatus.Completed,
    });
  }
  for (const item of attachments) {
    content.push({
      type: AgentScopeRuntimeContentType.FILE,
      file_id: item.attachment_id,
      file_name: item.filename,
      status: AgentScopeRuntimeRunStatus.Completed,
    });
  }
  if (!content.length) {
    throw new Error('request card requires user_input or attachments');
  }
  return content;
}

export function selectRequestInputText(data: IAgentScopeRuntimeRequest): string {
  return data.input
    .flatMap((message) => message.content)
    .flatMap((part) =>
      part.type === AgentScopeRuntimeContentType.TEXT && part.text ? [part.text] : [],
    )
    .join('\n');
}

export function selectRequestInputAttachments(data: IAgentScopeRuntimeRequest): AttachmentRef[] {
  return data.input
    .flatMap((message) => message.content)
    .flatMap((part) => {
      const attachment = attachmentRefFromRuntimeContent(part);
      return attachment ? [attachment] : [];
    });
}

export function resolveRequestAttachments(
  data: IAgentScopeRuntimeRequest,
  chats: readonly ConsoleChat[],
): AttachmentRef[] {
  const fromInput = selectRequestInputAttachments(data);
  if (fromInput.length) return fromInput;
  // Optimistic SDK bubbles carry created_at; rebuilt history cards do not.
  if (data.created_at == null) return [];

  const text = selectRequestInputText(data);
  for (let index = chats.length - 1; index >= 0; index -= 1) {
    const chat = chats[index];
    if (chat.status === 'running' && chat.user_input === text) {
      return chat.attachments ?? [];
    }
  }
  return [];
}

export function resolveRequestArtifactComments(
  data: IAgentScopeRuntimeRequest,
  chats: readonly ConsoleChat[],
): ArtifactComment[] {
  const text = selectRequestInputText(data);
  for (let index = chats.length - 1; index >= 0; index -= 1) {
    const chat = chats[index];
    if (chat.user_input === text) return chat.artifact_comments ?? [];
  }
  return [];
}

function requestCard(chat: ConsoleChat): IAgentScopeRuntimeWebUIMessage {
  return {
    id: `request-${chat.id}`,
    role: 'user',
    msgStatus: 'finished',
    cards: [
      {
        code: 'AgentScopeRuntimeRequestCard',
        data: {
          input: [
            {
              role: 'user',
              type: 'message',
              content: requestCardContent(chat),
            },
          ],
        },
      },
    ],
  };
}

export function timelineItemToRuntimeMessage(
  item: Exclude<ChatTimelineItem, { kind: 'user' }>,
  terminal: boolean,
): IAgentScopeRuntimeMessage {
  if (item.kind === 'raw') {
    const message = item.message;
    const messageStatus = getRuntimeResponseStatus(message.status);
    return {
      ...message,
      role: message.role ?? 'assistant',
      type: message.type as AgentScopeRuntimeMessageType,
      status:
        terminal && messageStatus === AgentScopeRuntimeRunStatus.InProgress
          ? AgentScopeRuntimeRunStatus.Completed
          : messageStatus,
      content: message.content.map((part) => ({
        ...part,
        status: terminal
          ? AgentScopeRuntimeRunStatus.Completed
          : getRuntimeResponseStatus(
              part.status === 'completed'
                ? 'completed'
                : part.status === 'failed'
                  ? 'failed'
                  : 'in_progress',
            ),
      })) as IContent[],
    };
  }

  if (item.kind === 'biz_event') {
    return createBizEventRuntimeMessage(item.event);
  }

  return createSegmentRuntimeMessage(item.segment, item.events);
}

function responseCard(projection: ChatStreamProjection): IAgentScopeRuntimeWebUIMessage {
  const { chat } = projection;
  const terminal = isTerminalChatStatus(chat.status);
  const status = terminal
    ? getRuntimeStatus(chat)
    : projection.responseStatus
      ? getRuntimeResponseStatus(projection.responseStatus)
      : getRuntimeStatus(chat);
  const output = selectChatTimeline(projection)
    .filter(
      (item): item is Exclude<ChatTimelineItem, { kind: 'user' }> =>
        item.kind !== 'user' &&
        !(item.kind === 'biz_event' && item.event.presentation?.card_type === 'user'),
    )
    .map((item) => timelineItemToRuntimeMessage(item, terminal));

  return {
    id: `assistant-${chat.id}`,
    role: 'assistant',
    msgStatus:
      status === AgentScopeRuntimeRunStatus.Completed
        ? 'finished'
        : status === AgentScopeRuntimeRunStatus.Failed
          ? 'error'
          : status === AgentScopeRuntimeRunStatus.Canceled
            ? 'interrupted'
            : 'generating',
    cards: [
      {
        code: 'AgentScopeRuntimeResponseCard',
        data: {
          id: projection.responseId ?? `response-${chat.id}`,
          object: 'response',
          status,
          created_at: chat.started_at ? Date.parse(chat.started_at) : Date.now(),
          completed_at: chat.completed_at ? Date.parse(chat.completed_at) : undefined,
          output,
          error: chat.error ?? undefined,
        } satisfies IAgentScopeRuntimeResponse,
      },
    ],
  };
}

type RuntimeCardCache = WeakMap<ChatStreamProjection, IAgentScopeRuntimeWebUIMessage[]>;

export function createRuntimeSessionBuilder() {
  const cards: RuntimeCardCache = new WeakMap();
  return (session: ConsoleSession, projections: ChatStreamProjection[]) =>
    buildRuntimeSession(session, projections, cards);
}

export function buildRuntimeSession(
  session: ConsoleSession,
  projections: ChatStreamProjection[],
  cache?: RuntimeCardCache,
): IAgentScopeRuntimeWebUISession {
  const messages = [...projections]
    .sort((left, right) => left.chat.sequence - right.chat.sequence)
    .flatMap((projection) => {
      const previous = cache?.get(projection);
      if (previous) return previous;
      const cards = [requestCard(projection.chat), responseCard(projection)];
      cache?.set(projection, cards);
      return cards;
    });

  return {
    id: session.id,
    name: session.title,
    messages,
    generating: projections.some(({ chat }) => chat.status === 'running'),
  };
}

export const draftRuntimeSession: IAgentScopeRuntimeWebUISession = {
  id: 'new',
  name: '新会话',
  messages: [],
  generating: false,
};

// The SDK creates a fresh response builder on reconnect. Incremental SSE may
// start with content or business events, so seed the same identity used by the
// snapshot card before forwarding those events. This is SDK-only initialization;
// it must not be applied to the projection or advance the server replay cursor.
export function createRuntimeStreamBootstrapEvent(
  chat: ConsoleChat,
  responseId: string | null = null,
): Extract<ConsoleStreamEvent, { object: 'response' }> {
  return {
    object: 'response',
    id: responseId ?? `response-${chat.id}`,
    status: 'in_progress',
    session_id: chat.session_id,
    chat_id: chat.id,
    sequence_number: chat.last_sequence_number,
    usage: null,
    error: null,
  };
}

function eventToRuntimeChunk(
  event: ConsoleStreamEvent,
): IAgentScopeRuntimeResponse | IAgentScopeRuntimeMessage | IContent {
  if (event.object === 'response') {
    const terminal = ['completed', 'failed', 'cancelled'].includes(event.status);
    return {
      id: event.id,
      object: 'response',
      status: getRuntimeResponseStatus(event.status),
      created_at: Date.now(),
      completed_at: terminal ? Date.now() : undefined,
      output: terminal ? [] : [createRuntimeStreamGuardMessage(event.id)],
      usage: event.usage ?? undefined,
      error: event.error ? { code: event.error.code, message: event.error.message } : undefined,
    };
  }
  if (event.object === 'message') {
    return {
      ...event,
      role: event.role ?? 'assistant',
      type: event.type as AgentScopeRuntimeMessageType,
      status: getRuntimeResponseStatus(event.status),
      content: event.content.map((content) => ({
        ...content,
        status:
          content.status === 'completed'
            ? AgentScopeRuntimeRunStatus.Completed
            : content.status === 'failed'
              ? AgentScopeRuntimeRunStatus.Failed
              : AgentScopeRuntimeRunStatus.InProgress,
      })) as IContent[],
    };
  }
  if (event.object === 'content') {
    return {
      ...event,
      status:
        event.status === 'completed'
          ? AgentScopeRuntimeRunStatus.Completed
          : event.status === 'failed'
            ? AgentScopeRuntimeRunStatus.Failed
            : AgentScopeRuntimeRunStatus.InProgress,
    } as unknown as IContent;
  }
  if (event.object === 'error') {
    return {
      id: `error-${event.sequence_number}`,
      object: 'message',
      role: 'assistant',
      type: AgentScopeRuntimeMessageType.ERROR,
      content: [],
      status: AgentScopeRuntimeRunStatus.Failed,
      code: event.code,
      message: event.message,
    };
  }
  return {
    id: `projection-${event.chat_id}-${event.sequence_number}`,
    object: 'message',
    role: 'assistant',
    type: AgentScopeRuntimeMessageType.HEARTBEAT,
    content: [],
    status: AgentScopeRuntimeRunStatus.InProgress,
  };
}

export function parseRuntimeStreamChunk(chunk: string) {
  return eventToRuntimeChunk(JSON.parse(chunk) as ConsoleStreamEvent);
}
