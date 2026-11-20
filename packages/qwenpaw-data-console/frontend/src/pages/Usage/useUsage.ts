import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { getHostAuthContext, subscribeHostAuth } from '@/services/auth';
import {
  getUsageBackgroundTasks,
  getUsageSessions,
  getUsageSummary,
  getUsageTimeseries,
} from '@/features/console/api/usage';
import type { UsagePeriod } from '@/features/console/api/usageTypes';
import { emptyUsageState, UsageController } from '@/features/console/model/usage/usageController';

export function useUsage() {
  const auth = useSyncExternalStore(subscribeHostAuth, getHostAuthContext);
  const [period, setPeriod] = useState<UsagePeriod>('7d');
  const [controller] = useState(
    () =>
      new UsageController(
        {
          summary: getUsageSummary,
          trend: getUsageTimeseries,
          sessions: getUsageSessions,
          background: getUsageBackgroundTasks,
        },
        (revision) => {
          const current = getHostAuthContext();
          return current.ready && current.revision === revision;
        },
      ),
  );
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  useEffect(() => {
    controller.setContext(auth.revision, auth.ready, period);
    return controller.stop;
  }, [auth.revision, auth.ready, period, controller]);
  // Mask old identity data during render, before the effect cancels old requests.
  const blank = useMemo(
    () => emptyUsageState(auth.revision, period, auth.ready),
    [auth.revision, auth.ready, period],
  );
  const state =
    snapshot.revision === auth.revision && snapshot.period === period && auth.ready
      ? snapshot
      : blank;
  return { state, period, setPeriod, refresh: controller.refresh, load: controller.load };
}
