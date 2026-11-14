import type { TFunction } from 'i18next';
import type {
  ProfilingOverallStatus,
  ProfilingRowStatus,
  ProfilingStageStatus,
  ProfilingTaskStatus,
} from '@/types/dataProfiling';

export type ProfilingStatusValue =
  ProfilingOverallStatus | ProfilingStageStatus | ProfilingTaskStatus | ProfilingRowStatus;

export const PROFILING_STATUS_COLORS: Record<string, string> = {
  NOT_STARTED: 'default',
  IN_PROGRESS: 'processing',
  PROBING: 'processing',
  REVIEWING: 'warning',
  COMPLETED: 'success',
  SKIPPED: 'default',
  RUNNING: 'processing',
  SUCCESS: 'success',
  STOPPED: 'default',
  FAILED: 'error',
  PENDING: 'warning',
  CONFIRMED: 'success',
};

export function statusText(t: TFunction, status?: ProfilingStatusValue | null): string {
  if (!status) return '-';
  return t(`dataProfiling.status.${status}`, { defaultValue: status });
}
