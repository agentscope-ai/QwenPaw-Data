import type {
  IDataContent,
  IAgentScopeRuntimeMessage,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { ChatStatus } from '../../api/types';

const toolInputTypes = new Set<string>([
  'function_call',
  'plugin_call',
  'tool_call',
  'component_call',
  'mcp_call',
]);

const toolOutputTypes = new Set<string>([
  'function_call_output',
  'plugin_call_output',
  'tool_call_output',
  'component_call_output',
  'mcp_call_output',
]);

export function isRuntimeResponseRunning(
  chatStatus: ChatStatus | undefined,
  sdkStatus: string,
): boolean {
  return chatStatus === undefined ? sdkStatus === 'in_progress' : chatStatus === 'running';
}

export function toolMessageKey(message: IAgentScopeRuntimeMessage): string {
  const content = message.content?.find(
    (item): item is IDataContent<{ call_id?: string; name?: string }> => item.type === 'data',
  );
  return content?.data.call_id ?? content?.data.name ?? message.id;
}

export function isRuntimeToolInputMessage(message: IAgentScopeRuntimeMessage): boolean {
  return toolInputTypes.has(message.type);
}

export function isRuntimeToolOutputMessage(message: IAgentScopeRuntimeMessage): boolean {
  return toolOutputTypes.has(message.type);
}

export function isRuntimeToolMessage(message: IAgentScopeRuntimeMessage): boolean {
  return isRuntimeToolInputMessage(message) || isRuntimeToolOutputMessage(message);
}

export function selectRuntimeProcessMessages(
  messages: IAgentScopeRuntimeMessage[],
): IAgentScopeRuntimeMessage[] {
  const result: IAgentScopeRuntimeMessage[] = [];
  const pendingToolIndexes = new Map<string, number>();

  messages.forEach((message) => {
    if (message.type === 'reasoning') {
      result.push(message);
      return;
    }

    if (toolInputTypes.has(message.type)) {
      pendingToolIndexes.set(toolMessageKey(message), result.length);
      result.push(message);
      return;
    }

    if (!toolOutputTypes.has(message.type)) return;

    const key = toolMessageKey(message);
    const inputIndex = pendingToolIndexes.get(key);
    if (inputIndex === undefined) {
      result.push(message);
      return;
    }

    const input = result[inputIndex];
    result[inputIndex] = {
      ...message,
      content: [...input.content, ...message.content],
    };
    pendingToolIndexes.delete(key);
  });

  return result;
}
