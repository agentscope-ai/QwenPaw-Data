/** API lists contain JSON data. Unchanged polls should not invalidate the view. */
export function preserveUnchangedList<T>(previous: T[], next: T[]): T[] {
  return JSON.stringify(previous) === JSON.stringify(next) ? previous : next;
}

// Scope each cache to an auth identity. Pending reads are shared; writes replace
// the entry so an older response cannot overwrite a newer value.
export function createReadCache<T>(ttlMs: number, now = Date.now) {
  type Entry = { promise: Promise<T>; expires: number };
  const entries = new Map<string, Entry>();
  const trim = () => {
    if (entries.size > 100) entries.delete(entries.keys().next().value!);
  };
  return {
    get(key: string, load: () => Promise<T>): Promise<T> {
      const current = entries.get(key);
      if (current && current.expires > now()) return current.promise;
      const entry: Entry = { promise: Promise.resolve().then(load), expires: Infinity };
      entries.set(key, entry);
      trim();
      entry.promise = entry.promise.then(
        (value) => {
          entry.expires = now() + ttlMs;
          return value;
        },
        (error) => {
          if (entries.get(key) === entry) entries.delete(key);
          throw error;
        },
      );
      return entry.promise;
    },
    set(key: string, value: T) {
      entries.set(key, { promise: Promise.resolve(value), expires: now() + ttlMs });
      trim();
    },
  };
}
