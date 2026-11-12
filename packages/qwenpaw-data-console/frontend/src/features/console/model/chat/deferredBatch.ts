// Coalesce neighboring HTTP completions without waiting for slower peers.
export function createDeferredBatch<T>(
  publish: (items: T[]) => void,
  schedule: (flush: () => void) => () => void = (flush) => {
    const timer = setTimeout(flush, 16);
    return () => clearTimeout(timer);
  },
) {
  let pending: T[] = [];
  let cancel: (() => void) | undefined;
  let disposed = false;
  const flush = () => {
    cancel?.();
    cancel = undefined;
    const batch = pending;
    pending = [];
    if (!disposed && batch.length) publish(batch);
  };
  return {
    push(item: T) {
      if (disposed) return;
      pending.push(item);
      cancel ??= schedule(flush);
    },
    flush,
    dispose() {
      disposed = true;
      cancel?.();
      cancel = undefined;
      pending = [];
    },
  };
}
