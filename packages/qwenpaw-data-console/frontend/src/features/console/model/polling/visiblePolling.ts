interface VisibilitySource {
  readonly visibilityState: string;
  addEventListener(type: 'visibilitychange', listener: () => void): void;
  removeEventListener(type: 'visibilitychange', listener: () => void): void;
}

/** Schedule after completion, pause in background, and never overlap visibility refreshes. */
export function startVisiblePolling(
  refresh: () => Promise<unknown>,
  intervalMs: number,
  visibility: VisibilitySource,
  immediate = true,
) {
  let stopped = false;
  let running = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clear = () => {
    clearTimeout(timer);
    timer = undefined;
  };
  const schedule = () => {
    clear();
    if (!stopped && visibility.visibilityState === 'visible')
      timer = setTimeout(() => void run(), intervalMs);
  };
  const run = async () => {
    if (stopped || running || visibility.visibilityState !== 'visible') return;
    clear();
    running = true;
    try {
      await refresh();
    } catch {
      /* Refresh owners expose their own errors; polling must remain retryable. */
    } finally {
      running = false;
      schedule();
    }
  };
  const onVisibility = () => {
    clear();
    if (visibility.visibilityState === 'visible') void run();
  };
  visibility.addEventListener('visibilitychange', onVisibility);
  if (immediate) void run();
  else schedule();
  return () => {
    stopped = true;
    clear();
    visibility.removeEventListener('visibilitychange', onVisibility);
  };
}

/** Share only an in-flight request; every later refresh still reads fresh server state. */
export function sharePendingRequest<T>(
  pending: Map<string, Promise<T>>,
  key: string,
  request: () => Promise<T>,
): Promise<T> {
  const existing = pending.get(key);
  if (existing) return existing;
  const result = request().finally(() => {
    if (pending.get(key) === result) pending.delete(key);
  });
  pending.set(key, result);
  return result;
}
