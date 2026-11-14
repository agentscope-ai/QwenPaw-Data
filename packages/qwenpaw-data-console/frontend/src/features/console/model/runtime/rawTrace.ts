import type { TraceMessage } from '../../api/types';

export interface RawTraceItem {
  id: string;
  input: TraceMessage;
  output: TraceMessage | null;
}

function isFailedMessage(message: TraceMessage): boolean {
  if (message.status === 'failed') return true;
  if (message.type !== 'plugin_call_output') return false;

  // Tool output messages can complete even when the tool result is a failure.
  return message.content.some((content) => {
    const data = content.data;
    if (content.type !== 'data' || !data || typeof data !== 'object' || Array.isArray(data)) {
      return false;
    }
    const state = (data as Record<string, unknown>).state;
    return state === 'error' || state === 'interrupted' || state === 'denied';
  });
}

export function isRawTraceItemFailed(item: RawTraceItem): boolean {
  return isFailedMessage(item.input) || (item.output !== null && isFailedMessage(item.output));
}

function pluginCallData(
  message: TraceMessage,
  field: 'call_id' | 'name',
): Record<string, unknown> | null {
  if (message.type !== 'plugin_call' && message.type !== 'plugin_call_output') return null;
  const data = message.content.find(
    (content) =>
      content.type === 'data' &&
      content.data &&
      typeof content.data === 'object' &&
      !Array.isArray(content.data) &&
      field in content.data,
  )?.data;
  return data && typeof data === 'object' && !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : null;
}

function pluginCallId(message: TraceMessage): string | null {
  const data = pluginCallData(message, 'call_id');
  if (!data) return null;
  const callId = data.call_id;
  return typeof callId === 'string' && callId.trim() ? callId : null;
}

export function getPluginCallToolName(message: TraceMessage): string | null {
  if (message.type !== 'plugin_call') return null;
  const name = pluginCallData(message, 'name')?.name;
  return typeof name === 'string' && name.trim() ? name.trim() : null;
}

export function selectRawTraceItems(messages: TraceMessage[]): RawTraceItem[] {
  const items: RawTraceItem[] = [];
  const pendingInputIndexes = new Map<string, number>();

  messages.forEach((message) => {
    const callId = pluginCallId(message);
    if (message.type === 'plugin_call' && callId) {
      const itemIndex = items.length;
      items.push({ id: message.id, input: message, output: null });
      // Duplicate active call IDs are malformed. Keep both visible and pair the
      // output with the first unmatched call so no raw message is swallowed.
      if (!pendingInputIndexes.has(callId)) pendingInputIndexes.set(callId, itemIndex);
      return;
    }

    if (message.type === 'plugin_call_output' && callId) {
      const inputIndex = pendingInputIndexes.get(callId);
      if (inputIndex !== undefined) {
        items[inputIndex] = { ...items[inputIndex], output: message };
        pendingInputIndexes.delete(callId);
        return;
      }
    }

    items.push({ id: message.id, input: message, output: null });
  });

  return items;
}
