import { request } from '../../../services/request.js';
import type {
  BackgroundTaskUsagePage,
  SessionUsagePage,
  UsagePageQuery,
  UsageQuery,
  UsageSummary,
  UsageTimeseries,
} from './usageTypes.js';

// 显式列出允许的字段，避免额外对象属性进入身份查询参数。
function usageQuery(query: UsageQuery): URLSearchParams {
  const params = new URLSearchParams({ period: query.period });
  if (query.timezone !== undefined) params.set('timezone', query.timezone);
  if (query.as_of !== undefined) params.set('as_of', query.as_of);
  return params;
}

function usagePageQuery(query: UsagePageQuery): URLSearchParams {
  const params = usageQuery(query);
  params.set('page', String(query.page));
  params.set('page_size', String(query.page_size));
  return params;
}

export function getUsageSummary(query: UsageQuery, signal?: AbortSignal): Promise<UsageSummary> {
  return request<UsageSummary>(`/usage/summary?${usageQuery(query)}`, { signal });
}

export function getUsageTimeseries(
  query: UsageQuery,
  signal?: AbortSignal,
): Promise<UsageTimeseries> {
  return request<UsageTimeseries>(`/usage/timeseries?${usageQuery(query)}`, { signal });
}

export function getUsageSessions(
  query: UsagePageQuery,
  signal?: AbortSignal,
): Promise<SessionUsagePage> {
  return request<SessionUsagePage>(`/usage/sessions?${usagePageQuery(query)}`, { signal });
}

export function getUsageBackgroundTasks(
  query: UsagePageQuery,
  signal?: AbortSignal,
): Promise<BackgroundTaskUsagePage> {
  return request<BackgroundTaskUsagePage>(`/usage/background-tasks?${usagePageQuery(query)}`, {
    signal,
  });
}
