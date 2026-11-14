import { getApiUrl } from '@/services/config';
import { fetchWithAuthRetry } from '@/services/auth';
import type { ConsoleStreamEvent } from './types';
import { drainSseFrames } from './sseFrames';

interface SubscribeChatEventsOptions {
  sessionId: string;
  chatId: string;
  afterSequenceNumber: number;
  signal: AbortSignal;
  onEvent: (event: ConsoleStreamEvent) => void;
}

export interface SubscribeChatEventsResult {
  reason: 'eof' | 'aborted' | 'dropped';
}

export async function subscribeChatEvents({
  sessionId,
  chatId,
  afterSequenceNumber,
  signal,
  onEvent,
}: SubscribeChatEventsOptions): Promise<SubscribeChatEventsResult> {
  const params = new URLSearchParams({
    after_sequence_number: String(afterSequenceNumber),
  });
  const response = await fetchWithAuthRetry(
    getApiUrl(`/sessions/${sessionId}/chats/${chatId}/events?${params}`),
    {
      headers: {
        Accept: 'text/event-stream',
      },
      signal,
    },
  );

  if (!response.ok || !response.body) {
    throw new Error(`SSE connection failed: ${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const parsed = drainSseFrames(buffer, done);
      buffer = parsed.remainder;
      parsed.events.forEach(onEvent);

      if (done) {
        return { reason: 'eof' };
      }
    }
  } catch {
    if (!signal.aborted) return { reason: 'dropped' };
  } finally {
    reader.releaseLock();
  }

  return { reason: 'aborted' };
}
