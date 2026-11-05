import type { ConsoleSession } from '../api/types';

export function selectHistorySessions(
  sessions: ConsoleSession[],
  currentSessionId: string | null,
  searchText: string,
): ConsoleSession[] {
  const normalizedSearch = searchText.trim().toLocaleLowerCase();

  return sessions.filter((session) => (
    session.id !== currentSessionId
    && session.chat_count > 0
    && (
      !normalizedSearch
      || session.title.toLocaleLowerCase().includes(normalizedSearch)
    )
  ));
}

export function selectSidebarSessions(
  sessions: ConsoleSession[],
  searchText: string,
): ConsoleSession[] {
  const normalizedSearch = searchText.trim().toLocaleLowerCase();

  return sessions.filter((session) => (
    session.chat_count > 0
    && (
      !normalizedSearch
      || session.title.toLocaleLowerCase().includes(normalizedSearch)
    )
  ));
}
