import type {
  IAgentScopeRuntimeWebUIMessage,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type {
  IAgentScopeRuntimeMessage,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { ConsoleStreamEvent, TraceMessage } from '../api/types';

export const runtimeSteerRequestIdPrefix = 'steer-request-';
const completedStatus = 'completed' as AgentScopeRuntimeRunStatus;
const inProgressStatus = 'in_progress' as AgentScopeRuntimeRunStatus;
const heartbeatType = 'heartbeat' as AgentScopeRuntimeMessageType;

export function createRuntimeStreamGuardMessage(
  responseId: string,
): IAgentScopeRuntimeMessage {
  return {
    id: `stream-guard-${responseId}`,
    object: 'message',
    role: 'assistant',
    type: heartbeatType,
    status: inProgressStatus,
    content: [],
  };
}

export function isSteerTraceMessage(message: TraceMessage): boolean {
  return message.role === 'user' && message.metadata?.source === 'steer';
}

export function createSteerRuntimeRequestCard(
  message: TraceMessage,
): IAgentScopeRuntimeWebUIMessage {
  const text = message.content.map((content) => content.text ?? '').join('');
  return {
    id: `${runtimeSteerRequestIdPrefix}${message.id}`,
    role: 'user',
    msgStatus: 'finished',
    cards: [{
      code: 'AgentScopeRuntimeRequestCard',
      data: {
        input: [{
          role: 'user',
          type: 'message',
          content: [{
            type: 'text',
            text,
            status: completedStatus,
          }],
        }],
      },
    }],
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
