import { useCallback, useEffect, useRef, useState } from "react";
import {
  modelConfigApi,
  type RebuildJob,
  type RebuildJobStatus,
} from "@/services/modelConfig";

const POLL_INTERVAL_MS = 2500;

export function isEmbeddingRebuildActive(
  status: RebuildJobStatus | undefined,
): boolean {
  return status === "pending" || status === "running";
}

interface UseEmbeddingRebuildOptions {
  onSuccess?: (job: RebuildJob) => void;
}

function createPendingJob(jobId: string): RebuildJob {
  return {
    job_id: jobId,
    status: "pending",
    created_at: new Date().toISOString(),
    progress: {
      phase: "",
      current_label: "",
      labels_done: 0,
      labels_total: 0,
    },
    config_snapshot: {},
  };
}

export function useEmbeddingRebuild({
  onSuccess,
}: UseEmbeddingRebuildOptions = {}) {
  const [job, setJob] = useState<RebuildJob | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [pollError, setPollError] = useState<string | null>(null);
  const onSuccessRef = useRef(onSuccess);
  const notifiedJobIdRef = useRef<string | null>(null);

  useEffect(() => {
    onSuccessRef.current = onSuccess;
  }, [onSuccess]);

  const restoreJob = useCallback((nextJob: RebuildJob | null) => {
    notifiedJobIdRef.current = nextJob?.status === "success" ? nextJob.job_id : null;
    setPollError(null);
    setJob(nextJob);
  }, []);

  const beginJob = useCallback((jobId: string) => {
    notifiedJobIdRef.current = null;
    setPollError(null);
    setJob(createPendingJob(jobId));
  }, []);

  const active = isEmbeddingRebuildActive(job?.status);
  const jobId = job?.job_id;

  useEffect(() => {
    if (!jobId || !active) return undefined;

    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const nextJob = await modelConfigApi.getRebuildJob(jobId);
        if (cancelled) return;

        setJob(nextJob);
        setPollError(null);

        if (
          nextJob.status === "success" &&
          notifiedJobIdRef.current !== nextJob.job_id
        ) {
          notifiedJobIdRef.current = nextJob.job_id;
          onSuccessRef.current?.(nextJob);
        }

        if (isEmbeddingRebuildActive(nextJob.status)) {
          timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        setPollError(error instanceof Error ? error.message : String(error));
        timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };

    timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, jobId]);

  const retry = useCallback(async () => {
    if (!job?.job_id || retrying) return;

    setRetrying(true);
    try {
      await modelConfigApi.retryRebuildJob(job.job_id);
      notifiedJobIdRef.current = null;
      setPollError(null);
      setJob((current) =>
        current
          ? {
              ...current,
              status: "pending",
              error: null,
              finished_at: null,
            }
          : createPendingJob(job.job_id),
      );
    } catch (error) {
      setPollError(error instanceof Error ? error.message : String(error));
    } finally {
      setRetrying(false);
    }
  }, [job?.job_id, retrying]);

  return {
    job,
    active,
    retrying,
    pollError,
    restoreJob,
    beginJob,
    retry,
  };
}
