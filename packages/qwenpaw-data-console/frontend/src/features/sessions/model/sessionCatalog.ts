import type { ConsoleSession } from '../../console/api/types';

export type SessionCatalogStatus = 'all' | ConsoleSession['status'];
export type SessionCatalogSort = 'updated_desc' | 'updated_asc' | 'chat_count_desc';

export interface SessionCatalogFilters {
  searchText: string;
  status: SessionCatalogStatus;
  datasourceId: string;
  sort: SessionCatalogSort;
}

export function selectCatalogSessions(
  sessions: ConsoleSession[],
  filters: SessionCatalogFilters,
): ConsoleSession[] {
  const normalizedSearch = filters.searchText.trim().toLocaleLowerCase();
  const visible = sessions.filter(
    (session) =>
      session.chat_count > 0 &&
      (filters.status === 'all' || session.status === filters.status) &&
      (filters.datasourceId === 'all' || session.datasource_id === filters.datasourceId) &&
      (!normalizedSearch || session.title.toLocaleLowerCase().includes(normalizedSearch)),
  );

  return [...visible].sort((left, right) => {
    if (filters.sort === 'chat_count_desc') {
      return (
        right.chat_count - left.chat_count ||
        Date.parse(right.updated_at) - Date.parse(left.updated_at)
      );
    }
    const direction = filters.sort === 'updated_asc' ? 1 : -1;
    return direction * (Date.parse(left.updated_at) - Date.parse(right.updated_at));
  });
}

export function formatSessionUpdatedAt(
  value: string,
  now = new Date(),
  language: 'zh' | 'en' = 'zh',
): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';

  const difference = now.getTime() - date.getTime();
  if (difference >= 0 && difference < 60_000) return language === 'zh' ? '刚刚' : 'Just now';
  if (difference >= 0 && difference < 3_600_000) {
    const minutes = Math.max(1, Math.floor(difference / 60_000));
    return language === 'zh' ? `${minutes} 分钟前` : `${minutes} min ago`;
  }

  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  const time = date.toLocaleTimeString(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return language === 'zh' ? `今天 ${time}` : `Today ${time}`;

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) {
    return language === 'zh' ? `昨天 ${time}` : `Yesterday ${time}`;
  }

  return date.toLocaleDateString(locale, {
    month: 'numeric',
    day: 'numeric',
  });
}
