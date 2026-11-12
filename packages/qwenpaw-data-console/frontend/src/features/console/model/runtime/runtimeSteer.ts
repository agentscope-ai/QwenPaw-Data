import type { IAgentScopeRuntimeWebUIMessage } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type {
  IAgentScopeRuntimeMessage,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { ConsoleStreamEvent, TraceMessage } from '../../api/types';
import { isTerminalChatStatus } from './chatStreamLifecycle.js';
import type { ChatStreamProjection } from './projectStream.js';

export const runtimeSteerRequestIdPrefix = 'steer-request-';
const completedStatus = 'completed' as AgentScopeRuntimeRunStatus;
const inProgressStatus = 'in_progress' as AgentScopeRuntimeRunStatus;
const heartbeatType = 'heartbeat' as AgentScopeRuntimeMessageType;

export function createRuntimeStreamGuardMessage(responseId: string): IAgentScopeRuntimeMessage {
  return {
    id: `stream-guard-${responseId}`,
    object: 'message',
    role: 'assistant',
    type: heartbeatType,
    status: inProgressStatus,
    content: [],
  };
}

export function createSteerRuntimeRequestCard(
  message: TraceMessage,
): IAgentScopeRuntimeWebUIMessage {
  const text = message.content.map((content) => content.text ?? '').join('');
  return {
    id: `${runtimeSteerRequestIdPrefix}${message.id}`,
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
              content: [
                {
                  type: 'text',
                  text,
                  status: completedStatus,
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

/**
 * Steer hints belong to the chat timeline, but they are not assistant output.
 * Feeding them into AgentScope's response builder can make an in-progress
 * response look terminal when every preceding tool message is complete.
 */
export function shouldForwardRuntimeStreamEvent(
  event: ConsoleStreamEvent,
  steerMessageIds: Set<string>,
): boolean {
  // The host projects MCP failures into timeline tool cards; SDK does not consume this event.
  if (event.object === 'mcp.connect') return false;
  if (event.object === 'model.call') return false;
  if (
    event.object === 'model.call.end' ||
    event.object === 'tool.result.start' ||
    event.object === 'tool.result.end'
  )
    return false;

  if (event.object === 'message') {
    if (event.role === 'user' && event.metadata?.source === 'steer') {
      steerMessageIds.add(event.id);
      return false;
    }
    return !steerMessageIds.has(event.id);
  }

  if (event.object === 'content') {
    return !steerMessageIds.has(event.msg_id);
  }

  return true;
}

export function isSteerUserMessage(message: Pick<TraceMessage, 'role' | 'metadata'>): boolean {
  return message.role === 'user' && message.metadata?.source === 'steer';
}

export class SteerChatEndedError extends Error {
  override name = 'SteerChatEndedError';
}

export class SteerArrivalAbortedError extends Error {
  override name = 'SteerArrivalAbortedError';
}

export function createSteerArrival() {
  const knownIds = new Set<string>();
  const waiters: Array<{
    chatId: string;
    resolve: () => void;
    reject: (error: Error) => void;
  }> = [];

  function steerIds(projection: ChatStreamProjection): string[] {
    return projection.messages.filter(isSteerUserMessage).map((message) => message.id);
  }

  function fail(chatId: string, error: Error): void {
    const remaining: typeof waiters = [];
    for (const waiter of waiters) {
      if (waiter.chatId === chatId) waiter.reject(error);
      else remaining.push(waiter);
    }
    waiters.splice(0, waiters.length, ...remaining);
  }

  function failAll(error: Error): void {
    const pending = waiters.splice(0, waiters.length);
    for (const waiter of pending) waiter.reject(error);
  }

  function remember(projection: ChatStreamProjection): void {
    for (const id of steerIds(projection)) knownIds.add(id);
  }

  function observe(projection: ChatStreamProjection): void {
    for (const id of steerIds(projection)) {
      if (knownIds.has(id)) continue;
      knownIds.add(id);
      const index = waiters.findIndex((waiter) => waiter.chatId === projection.chat.id);
      if (index < 0) continue;
      const [waiter] = waiters.splice(index, 1);
      waiter.resolve();
    }
    if (isTerminalChatStatus(projection.chat.status)) {
      fail(projection.chat.id, new SteerChatEndedError());
    }
  }

  function wait(chatId: string): Promise<void> {
    return new Promise((resolve, reject) => {
      waiters.push({ chatId, resolve, reject });
    });
  }

  return { remember, observe, wait, fail, failAll };
}
