import type { ChatStatus, PendingToolConfirmation, TraceMessage } from '../../api/types.js';

function toolData(message: TraceMessage): Record<string, unknown> | null {
  const data = message.content.find((item) => item.type === 'data')?.data;
  return data && typeof data === 'object' && !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : null;
}

function nonempty(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim());
}

export function toolConfirmationCallId(message: TraceMessage): string | null {
  const value = toolData(message)?.call_id;
  return nonempty(value) ? value : nonempty(message.source_id) ? message.source_id : null;
}

export function hasToolConfirmationOutput(
  messages: TraceMessage[],
  chatId: string,
  callId: string,
) {
  return messages.some(
    (message) =>
      message.chat_id === chatId &&
      isToolConfirmationResult(message) &&
      toolConfirmationCallId(message) === callId,
  );
}

// Host opens the output message BEFORE the approval middleware finishes waiting.
// An empty start/envelope is not evidence of execution or a resolved decision.
export function isToolConfirmationResult(message: TraceMessage): boolean {
  if (message.type !== 'plugin_call_output') return false;
  if (['completed', 'failed', 'cancelled'].includes(message.status)) return true;
  return message.content.some((content) => {
    if (content.type === 'text') return Boolean(content.text?.trim());
    if (content.type !== 'data')
      return Boolean(content.image_url || content.file_url || content.video_url);
    const data = content.data;
    if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
    const output = (data as Record<string, unknown>).output;
    if (typeof output === 'string') return Boolean(output.trim());
    if (Array.isArray(output)) return output.length > 0;
    if (output && typeof output === 'object') return Object.keys(output).length > 0;
    return output !== undefined && output !== null;
  });
}

export function selectToolConfirmations(
  messages: TraceMessage[],
  chatId: string,
): PendingToolConfirmation[] {
  const calls = new Map<string, PendingToolConfirmation>();
  for (const message of [...messages].sort((a, b) => a.sequence - b.sequence)) {
    if (
      message.chat_id !== chatId ||
      message.type !== 'plugin_call' ||
      message.status !== 'completed' ||
      message.metadata?.awaiting_confirmation !== true
    )
      continue;
    const data = toolData(message);
    const callId = toolConfirmationCallId(message);
    if (
      !data ||
      !callId ||
      !nonempty(data.name) ||
      data.name === 'ask_user_question' ||
      data.arguments === undefined ||
      data.arguments === null
    )
      continue;
    if (hasToolConfirmationOutput(messages, chatId, callId)) continue;
    let args: unknown = data.arguments;
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args);
      } catch {
        /* Preserve non-JSON arguments as text. */
      }
    }
    calls.set(callId, {
      callId,
      chatId,
      name: data.name,
      argumentsText: typeof args === 'string' ? args : JSON.stringify(args, null, 2),
      expiresAt:
        typeof message.metadata.expires_at === 'string' ? message.metadata.expires_at : null,
    });
  }
  return [...calls.values()];
}

export function visibleToolConfirmations(
  candidates: PendingToolConfirmation[],
  context: { status: ChatStatus | undefined; stopping: boolean },
  now = Date.now(),
): PendingToolConfirmation[] {
  if (context.status !== 'running' || context.stopping) return [];
  // Hide expired approvals even for read-only viewers or while history reloads.
  // This is presentation only: never submit a decision or clear a submission lock.
  return candidates.filter((candidate) => {
    const deadline = candidate.expiresAt ? Date.parse(candidate.expiresAt) : NaN;
    return !Number.isFinite(deadline) || deadline > now;
  });
}

export function toolConfirmationAvailability(
  candidate: PendingToolConfirmation,
  context: { status: ChatStatus; readOnly: boolean; stopping: boolean; ready: boolean },
  now = Date.now(),
): 'pending' | 'loading' | 'readOnly' | 'ended' | 'expired' | 'unavailable' {
  if (!context.ready) return 'loading';
  if (context.readOnly) return 'readOnly';
  if (context.status !== 'running' || context.stopping) return 'ended';
  if (candidate.expiresAt) {
    const deadline = Date.parse(candidate.expiresAt);
    if (!Number.isFinite(deadline)) return 'unavailable';
    if (deadline <= now) return 'expired';
  }
  return 'pending';
}
