import type { TraceMessage } from '../../api/types';
import { isTerminalChatStatus } from './chatStreamLifecycle.js';
import type { ChatStreamProjection } from './projectStream.js';
import { isRawTraceItemFailed } from './rawTrace.js';

export interface SubagentActivity {
  parentCallId: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  messages: TraceMessage[];
  error?: string;
}

export function selectSubagentActivities(projection: ChatStreamProjection) {
  const groups = new Map<string, TraceMessage[]>();
  const parentResults = new Map<string, TraceMessage>();
  for (const message of projection.messages) {
    if (message.stream?.type === 'subagent') {
      const id = message.stream.parent_call_id;
      const group = groups.get(id) ?? [];
      group.push(message);
      groups.set(id, group);
    } else if (message.type === 'plugin_call_output' && message.source_id) {
      parentResults.set(message.source_id, message);
    }
  }
  for (const id of projection.subagentResponses?.keys() ?? []) {
    if (!groups.has(id)) groups.set(id, []);
  }
  const activities = new Map<string, SubagentActivity>();
  for (const [id, messages] of groups) {
    const response = projection.subagentResponses?.get(id);
    const result = parentResults.get(id);
    let status: SubagentActivity['status'] = 'running';
    if (response?.status === 'cancelled' || result?.status === 'cancelled') {
      status = 'cancelled';
    } else if (
      response?.status === 'failed' ||
      (result && isRawTraceItemFailed({ id: result.id, input: result, output: null }))
    ) {
      status = 'failed';
    } else if (response?.status === 'completed' || result?.status === 'completed') {
      status = 'completed';
    } else if (isTerminalChatStatus(projection.chat.status)) {
      status = projection.chat.status === 'completed' ? 'completed' : 'cancelled';
    }
    activities.set(id, {
      parentCallId: id,
      status,
      messages: messages.sort((left, right) => left.sequence - right.sequence),
      error: response?.error,
    });
  }
  return activities;
}
