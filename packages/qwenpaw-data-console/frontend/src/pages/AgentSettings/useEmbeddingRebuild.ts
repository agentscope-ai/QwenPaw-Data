import { useCallback, useEffect, useRef, useState } from 'react';
import { modelConfigApi } from '@/features/console/api/modelConfig';
import type { RebuildJob } from '@/features/console/api/modelConfigTypes';

export function useEmbeddingRebuild(revision: number, ready: boolean) {
  const [job, setJob] = useState<RebuildJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const lifetime = useRef<AbortController>();
  const active = job?.status === 'pending' || job?.status === 'running';

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    setJob(null);
    setBusy(false);
    setError(null);
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await modelConfigApi.getLatestRebuildJob(controller.signal);
        if (controller.signal.aborted) return;
        setJob(next);
        setError(null);
        if (next?.status === 'pending' || next?.status === 'running')
          timer = setTimeout(poll, 2500);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : String(cause));
          timer = setTimeout(poll, 2500);
        }
      }
    };
    if (ready) void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [revision, ready, refresh]);

  const run = useCallback(
    async (retry = false) => {
      const controller = lifetime.current;
      if (!controller || controller.signal.aborted || busy || active || !ready) return;
      setBusy(true);
      setError(null);
      try {
        if (retry && job) await modelConfigApi.retryRebuildJob(job.job_id, controller.signal);
        else await modelConfigApi.rebuildEmbeddings(controller.signal);
        if (!controller.signal.aborted) setRefresh((value) => value + 1);
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [busy, active, ready, job],
  );

  return { job, busy, error, active, run };
}
