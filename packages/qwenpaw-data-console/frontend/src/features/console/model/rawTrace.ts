import type { TraceMessage } from '../api/types';

export interface RawTraceItem {
  id: string;
  input: TraceMessage;
  output: TraceMessage | null;
}

function pluginCallId(message: TraceMessage): string | null {
  if (message.type !== 'plugin_call' && message.type !== 'plugin_call_output') return null;
  const data = message.content.find((content) => (
    content.type === 'data'
    && content.data
    && typeof content.data === 'object'
    && 'call_id' in content.data
  ))?.data;
  if (!data || typeof data !== 'object' || !('call_id' in data)) return null;
  const callId = data.call_id;
  return typeof callId === 'string' && callId.trim() ? callId : null;
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
