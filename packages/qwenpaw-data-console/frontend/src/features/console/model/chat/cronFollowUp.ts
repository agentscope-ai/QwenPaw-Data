import type { ConsoleChat, TraceMessage } from '../../api/types';

export interface CronFollowUpExpectation {
  jobId: string;
  message: string;
  sessionId: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isCronJobToolName(value: unknown): boolean {
  return typeof value === 'string' && value.replace(/[^a-z0-9]/gi, '').toLowerCase() === 'cronjob';
}

function parseObject(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Find the latest successful CronJob run that promises another Chat in the
 * current Console Session. A create/replace result also contains a job, but
 * only run returns `{ ok: true, job }`.
 */
export function selectCronFollowUpExpectation(
  messages: readonly TraceMessage[],
  currentSessionId: string,
): CronFollowUpExpectation | null {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
    const message = messages[messageIndex];
    if (message.type !== 'plugin_call_output' || message.status !== 'completed') {
      continue;
    }

    for (let contentIndex = message.content.length - 1; contentIndex >= 0; contentIndex -= 1) {
      const content = message.content[contentIndex];
      if (content.type !== 'data' || !isRecord(content.data)) continue;
      if (!isCronJobToolName(content.data.name)) continue;

      const result = parseObject(content.data.output);
      const job = result && isRecord(result.job) ? result.job : null;
      if (
        result?.ok !== true ||
        !job ||
        job.session_id !== currentSessionId ||
        job.channel !== 'console' ||
        typeof job.id !== 'string' ||
        typeof job.message !== 'string' ||
        !job.message
      ) {
        continue;
      }

      return {
        jobId: job.id,
        message: job.message,
        sessionId: currentSessionId,
      };
    }
  }

  return null;
}

export function selectCronFollowUpChat(
  chats: readonly ConsoleChat[],
  expectation: CronFollowUpExpectation,
  afterSequence: number,
): ConsoleChat | null {
  let selected: ConsoleChat | null = null;
  for (const chat of chats) {
    if (
      chat.sequence <= afterSequence ||
      chat.session_id !== expectation.sessionId ||
      chat.user_input !== expectation.message
    ) {
      continue;
    }
    if (!selected || chat.sequence < selected.sequence) selected = chat;
  }
  return selected;
}
