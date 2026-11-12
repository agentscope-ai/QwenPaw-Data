import { request } from '../../../services/request.js';
import type { ConsoleChat, ToolConfirmationDecision } from './types.js';

export async function answerToolConfirmation(
  sessionId: string,
  chatId: string,
  body: { call_id: string; decision: ToolConfirmationDecision },
) {
  const response = await request<{ chat: ConsoleChat }>(
    `/sessions/${encodeURIComponent(sessionId)}/chats/${encodeURIComponent(chatId)}/tool-confirmation/answer`,
    { method: 'POST', body: JSON.stringify(body) },
  );
  // A successful HTTP response alone cannot establish that this decision was accepted.
  if (response?.chat?.id !== chatId || response?.chat?.session_id !== sessionId) {
    throw new Error('Invalid tool confirmation response');
  }
  return response;
}
