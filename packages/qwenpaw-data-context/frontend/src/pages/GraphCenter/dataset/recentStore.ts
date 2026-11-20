/** Recently visited nodes (demo: in-memory list, max 6). Kept module-scoped on
 *  purpose — survives tab switches like the original App.recent. */
const MAX_RECENT = 6;
const recent: string[] = [];

export function pushRecent(id: string) {
  const i = recent.indexOf(id);
  if (i >= 0) recent.splice(i, 1);
  recent.unshift(id);
  if (recent.length > MAX_RECENT) recent.length = MAX_RECENT;
}

export function getRecent(): string[] {
  return recent;
}
