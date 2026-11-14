import type {
  BackgroundTaskUsagePage,
  SessionUsagePage,
  UsagePageQuery,
  UsagePeriod,
  UsageQuery,
  UsageSummary,
  UsageTimeseries,
  UsageWindow,
} from '../../api/usageTypes.js';

export type UsageError = 'validation' | 'auth' | 'forbidden' | 'load';
export interface UsageRegion<T> {
  data: T | null;
  loading: boolean;
  error: UsageError | null;
}
interface DetailData {
  trend: UsageTimeseries;
  sessions: SessionUsagePage;
  background: BackgroundTaskUsagePage;
}
export type UsageDetail = keyof DetailData;
export interface UsageState {
  revision: number;
  period: UsagePeriod;
  ready: boolean;
  summary: UsageRegion<UsageSummary>;
  trend: UsageRegion<UsageTimeseries>;
  sessions: UsageRegion<SessionUsagePage>;
  background: UsageRegion<BackgroundTaskUsagePage>;
  sessionPage: number;
  backgroundPage: number;
}
export interface UsageApi {
  summary: (query: UsageQuery, signal: AbortSignal) => Promise<UsageSummary>;
  trend: (query: UsageQuery, signal: AbortSignal) => Promise<UsageTimeseries>;
  sessions: (query: UsagePageQuery, signal: AbortSignal) => Promise<SessionUsagePage>;
  background: (query: UsagePageQuery, signal: AbortSignal) => Promise<BackgroundTaskUsagePage>;
}

const emptyRegion = <T>(): UsageRegion<T> => ({ data: null, loading: false, error: null });

export function emptyUsageState(revision: number, period: UsagePeriod, ready: boolean): UsageState {
  return {
    revision,
    period,
    ready,
    summary: emptyRegion(),
    trend: emptyRegion(),
    sessions: emptyRegion(),
    background: emptyRegion(),
    sessionPage: 1,
    backgroundPage: 1,
  };
}

export function usageError(cause: unknown): UsageError {
  if (cause && typeof cause === 'object') {
    const error = cause as { status?: number; code?: string };
    if (error.status === 422 || error.code === 'VALIDATION') return 'validation';
    if (error.status === 401) return 'auth';
    if (error.status === 403) return 'forbidden';
  }
  return 'load';
}

/** A page-owned store; never shares usage across users or workspace revisions. */
export class UsageController {
  private state = emptyUsageState(-1, '7d', false);
  private listeners = new Set<() => void>();
  private epoch = 0;
  private window: UsageWindow | null = null;
  private requests: Partial<Record<'summary' | UsageDetail, AbortController>> = {};

  constructor(
    private api: UsageApi,
    private isIdentityCurrent: (revision: number) => boolean = () => true,
  ) {}

  getSnapshot = (): UsageState => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private publish(patch: Partial<UsageState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  private cancel() {
    this.epoch += 1;
    Object.values(this.requests).forEach((request) => request.abort());
    this.requests = {};
  }

  stop = () => {
    this.cancel();
    this.window = null;
    this.publish(emptyUsageState(this.state.revision, this.state.period, false));
  };

  setContext(revision: number, ready: boolean, period: UsagePeriod) {
    if (
      this.state.revision === revision &&
      this.state.ready === ready &&
      this.state.period === period
    )
      return;
    this.cancel();
    this.window = null;
    this.publish(emptyUsageState(revision, period, ready));
    if (ready) void this.refresh();
  }

  private current(epoch: number, request: AbortController) {
    return (
      this.state.ready &&
      !request.signal.aborted &&
      epoch === this.epoch &&
      this.isIdentityCurrent(this.state.revision)
    );
  }

  refresh = async () => {
    if (!this.state.ready || !this.isIdentityCurrent(this.state.revision)) return;
    this.cancel();
    const epoch = this.epoch;
    const request = new AbortController();
    this.requests.summary = request;
    this.publish({
      summary: { ...this.state.summary, loading: true, error: null },
      trend: { ...this.state.trend, loading: false },
      sessions: { ...this.state.sessions, loading: false },
      background: { ...this.state.background, loading: false },
    });
    try {
      const data = await this.api.summary({ period: this.state.period }, request.signal);
      if (!this.current(epoch, request)) return;
      this.window = data.window;
      this.publish({
        summary: { data, loading: false, error: null },
        trend: emptyRegion(),
        sessions: emptyRegion(),
        background: emptyRegion(),
        sessionPage: 1,
        backgroundPage: 1,
      });
      // Independent regions settle independently; a slow table cannot hold the chart.
      void this.load('trend');
      void this.load('sessions');
      void this.load('background');
    } catch (cause) {
      if (!this.current(epoch, request)) return;
      this.publish({
        summary: { ...this.state.summary, loading: false, error: usageError(cause) },
      });
    }
  };

  load = async <K extends UsageDetail>(key: K, page?: number) => {
    if (
      !this.window ||
      this.state.summary.loading ||
      !this.state.ready ||
      !this.isIdentityCurrent(this.state.revision)
    )
      return;
    this.requests[key]?.abort();
    const request = new AbortController();
    this.requests[key] = request;
    const epoch = this.epoch;
    const requestedPage =
      page ?? (key === 'sessions' ? this.state.sessionPage : this.state.backgroundPage);
    const query: UsagePageQuery = {
      period: this.window.period,
      timezone: this.window.timezone,
      as_of: this.window.as_of,
      page: requestedPage,
      page_size: 10,
    };
    this.publish({
      [key]: { ...this.state[key], loading: true, error: null },
      ...(key === 'sessions' ? { sessionPage: requestedPage } : {}),
      ...(key === 'background' ? { backgroundPage: requestedPage } : {}),
    });
    try {
      const data = await this.api[key](query, request.signal);
      if (!this.current(epoch, request)) return;
      this.publish({ [key]: { data, loading: false, error: null } });
    } catch (cause) {
      if (!this.current(epoch, request)) return;
      this.publish({ [key]: { ...this.state[key], loading: false, error: usageError(cause) } });
    }
  };
}
