import { request } from '@/services/request';

export type CronScheduleType = 'cron' | 'once';

export interface CronSchedule {
  type: CronScheduleType;
  cron: string | null;
  run_at: string | null;
  timezone: string;
}

export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  message: string;
  datasource_id: string;
  schedule: CronSchedule;
  created_at: string;
  updated_at: string;
}

export interface CronJobWriteBody {
  name: string;
  enabled: boolean;
  message: string;
  datasource_id: string;
  schedule: CronSchedule;
}

export interface CronJobDraft {
  name: string;
  message: string;
  datasource_id: string;
}

export const cronApi = {
  listJobs() {
    return request<{ jobs: CronJob[]; count: number }>('/cron/jobs');
  },

  getJob(jobId: string) {
    return request<{ job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}`,
    );
  },

  createJob(body: CronJobWriteBody) {
    return request<{ job: CronJob }>('/cron/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  replaceJob(jobId: string, body: CronJobWriteBody) {
    return request<{ job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}`,
      {
        method: 'PUT',
        body: JSON.stringify(body),
      },
    );
  },

  deleteJob(jobId: string) {
    return request<{ ok: boolean }>(
      `/cron/jobs/${encodeURIComponent(jobId)}`,
      { method: 'DELETE' },
    );
  },

  pauseJob(jobId: string) {
    return request<{ job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}/pause`,
      { method: 'POST' },
    );
  },

  resumeJob(jobId: string) {
    return request<{ job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}/resume`,
      { method: 'POST' },
    );
  },

  runJob(jobId: string) {
    return request<{ ok: boolean; job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}/run`,
      { method: 'POST' },
    );
  },
};
