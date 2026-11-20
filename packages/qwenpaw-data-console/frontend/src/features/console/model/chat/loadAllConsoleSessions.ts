import type { ConsoleSession } from '../../api/types';

const SESSION_PAGE_SIZE = 100;

interface ListSessionsResponse {
  items: ConsoleSession[];
  total: number;
}

type ListSessions = (params: {
  sort: 'updated_desc';
  page: number;
  page_size: number;
}) => Promise<ListSessionsResponse>;

export async function loadAllConsoleSessions(
  listSessions: ListSessions,
  signal?: AbortSignal,
): Promise<ConsoleSession[]> {
  const first = await listSessions({
    sort: 'updated_desc',
    page: 1,
    page_size: SESSION_PAGE_SIZE,
  });
  signal?.throwIfAborted();
  const pageCount = Math.ceil(first.total / SESSION_PAGE_SIZE);
  if (pageCount <= 1) return first.items;

  const remaining = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) =>
      listSessions({
        sort: 'updated_desc',
        page: index + 2,
        page_size: SESSION_PAGE_SIZE,
      }),
    ),
  );

  return first.items.concat(remaining.flatMap(({ items }) => items));
}
