import { request } from '../../../services/request.js';
import type { UiOutcome } from '../ui-runtime/contracts.js';
import type { ConsoleChat } from './types.js';

export async function answerUi(
  sessionId: string,
  chatId: string,
  body: { call_id: string } & UiOutcome,
  signal?: AbortSignal,
) {
  const response = await request<{ chat: ConsoleChat }>(
    `/sessions/${encodeURIComponent(sessionId)}/chats/${encodeURIComponent(chatId)}/ui/answer`,
    { method: 'POST', body: JSON.stringify(body), signal },
  );
  if (response?.chat?.id !== chatId || response?.chat?.session_id !== sessionId) {
    throw new Error('Invalid UI answer response');
  }
  return response;
}
