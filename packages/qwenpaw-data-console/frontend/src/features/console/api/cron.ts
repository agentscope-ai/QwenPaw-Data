import { request } from '@/services/request';

export type CronScheduleType = 'cron' | 'once';

export interface CronSchedule {
  type: CronScheduleType;
  cron: string | null;
  run_at: string | null;
  timezone: string;
}

export const CRON_CHANNELS = ['console', 'feishu', 'dingtalk', 'wecom', 'wechat'] as const;
export type CronChannel = (typeof CRON_CHANNELS)[number];
export const CRON_IM_CHANNELS = ['feishu', 'dingtalk', 'wecom', 'wechat'] as const;

export function isCronChannel(channel: string): channel is CronChannel {
  return (CRON_CHANNELS as readonly string[]).includes(channel);
}

export function isCronImChannel(channel: string): boolean {
  return (CRON_IM_CHANNELS as readonly string[]).includes(channel);
}

export interface CronTarget {
  external_key: string;
  target_type: string;
  display_name: string;
  last_active_at: string;
}

export type CronSessionPolicy = 'reuse' | 'new_each_run';

export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  message: string;
  datasource_id: string;
  channel: string;
  session_policy: CronSessionPolicy;
  session_id: string | null;
  target_external_key: string | null;
  attachments: string[];
  schedule: CronSchedule;
  created_at: string;
  updated_at: string;
}

export interface CronJobWriteBody {
  name: string;
  enabled: boolean;
  message: string;
  datasource_id: string;
  channel: string;
  session_policy: CronSessionPolicy;
  target_external_key?: string | null;
  schedule: CronSchedule;
}

export interface CronJobDraft {
  name: string;
  message: string;
  datasource_id: string;
  channel?: string;
}

export interface CronJobListParams {
  keyword?: string;
  enabled?: boolean;
  channel?: string;
}

export const cronApi = {
  listJobs(params?: CronJobListParams, signal?: AbortSignal) {
    const search = new URLSearchParams();
    if (params) {
      if (params.keyword) search.set('keyword', params.keyword);
      if (params.enabled !== undefined) search.set('enabled', params.enabled.toString());
      if (params.channel) search.set('channel', params.channel);
    }
    const query = search.toString();
    return request<{ jobs: CronJob[]; count: number }>(
      `/cron/jobs${query ? `?${query}` : ''}`,
      signal ? { signal } : {},
    );
  },

  getJob(jobId: string) {
    return request<{ job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}`);
  },

  createJob(body: CronJobWriteBody) {
    return request<{ job: CronJob }>('/cron/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  replaceJob(jobId: string, body: CronJobWriteBody) {
    return request<{ job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  deleteJob(jobId: string) {
    return request<{ ok: boolean }>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: 'DELETE',
    });
  },

  pauseJob(jobId: string) {
    return request<{ job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}/pause`, {
      method: 'POST',
    });
  },

  resumeJob(jobId: string) {
    return request<{ job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}/resume`, {
      method: 'POST',
    });
  },

  runJob(jobId: string) {
    return request<{ ok: boolean; job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}/run`, {
      method: 'POST',
    });
  },

  listTargets(channel: string) {
    const search = new URLSearchParams({ channel });
    return request<{ targets: CronTarget[]; count: number }>(`/cron/targets?${search.toString()}`);
  },

  uploadAttachment(jobId: string, file: File) {
    const body = new FormData();
    body.append('file', file);
    return request<{ job: CronJob }>(`/cron/jobs/${encodeURIComponent(jobId)}/attachments`, {
      method: 'POST',
      body,
    });
  },

  deleteAttachment(jobId: string, filename: string) {
    return request<{ job: CronJob }>(
      `/cron/jobs/${encodeURIComponent(jobId)}/attachments/${encodeURIComponent(filename)}`,
      { method: 'DELETE' },
    );
  },
};
