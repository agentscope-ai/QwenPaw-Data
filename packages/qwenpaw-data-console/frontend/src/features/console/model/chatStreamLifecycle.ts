import type { ConsoleChat, SessionSnapshot } from '../api/types';

export type ChatStreamCompletionAction =
  | 'aborted'
  | 'terminal'
  | 'reconnect'
  | 'unexpected';

const terminalChatStatuses = new Set<ConsoleChat['status']>([
  'completed',
  'failed',
  'canceled',
]);

export function isTerminalChatStatus(status: ConsoleChat['status']): boolean {
  return terminalChatStatuses.has(status);
}

export function isSnapshotSessionRunning(
  snapshot: Pick<SessionSnapshot, 'session' | 'chats'>,
): boolean {
  return snapshot.session.status === 'running'
    || snapshot.chats.some((chat) => chat.status === 'running');
}

export function selectChatStreamCompletionAction(
  reason: 'eof' | 'aborted',
  chatStatus: ConsoleChat['status'] | null,
): ChatStreamCompletionAction {
  if (reason === 'aborted') return 'aborted';
  if (chatStatus && isTerminalChatStatus(chatStatus)) return 'terminal';
  if (chatStatus === 'running') return 'reconnect';
  return 'unexpected';
}

export interface StreamSettlementGuard {
  isSettled: () => boolean;
  settle: () => boolean;
}

export function createStreamSettlementGuard(onSettle: () => void): StreamSettlementGuard {
  let settled = false;
  return {
    isSettled: () => settled,
    settle: () => {
      if (settled) return false;
      settled = true;
      onSettle();
      return true;
    },
  };
}
