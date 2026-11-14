import { useEffect, useRef } from 'react';
import {
  PROVISION_POLL_INTERVAL_MS,
  PROVISION_POLL_MAX_MS,
  provisionStatusKey,
  shouldPollProvisioning,
  type ProvisionPollItem,
} from '@/utils/provisionPoll';

export function useProvisionStatusPoll(options: {
  enabled: boolean;
  startedAtRef: React.MutableRefObject<number | null>;
  lastKeyRef: React.MutableRefObject<string>;
  fetchItems: () => Promise<ProvisionPollItem[]>;
  onChanged: () => void;
  onTimeout: () => void;
  onFailed: () => void;
  onIdle: () => void;
}): void {
  const fetchItemsRef = useRef(options.fetchItems);
  const onChangedRef = useRef(options.onChanged);
  const onTimeoutRef = useRef(options.onTimeout);
  const onFailedRef = useRef(options.onFailed);
  const onIdleRef = useRef(options.onIdle);
  fetchItemsRef.current = options.fetchItems;
  onChangedRef.current = options.onChanged;
  onTimeoutRef.current = options.onTimeout;
  onFailedRef.current = options.onFailed;
  onIdleRef.current = options.onIdle;

  const inFlightRef = useRef(false);
  const failRef = useRef(0);

  useEffect(() => {
    if (!options.enabled) {
      failRef.current = 0;
      return;
    }

    const tick = async () => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const items = await fetchItemsRef.current();
        failRef.current = 0;
        if (
          options.startedAtRef.current != null &&
          Date.now() - options.startedAtRef.current > PROVISION_POLL_MAX_MS
        ) {
          onTimeoutRef.current();
          return;
        }
        const key = provisionStatusKey(items);
        if (key !== options.lastKeyRef.current) {
          onChangedRef.current();
          return;
        }
        if (!shouldPollProvisioning(items)) {
          onIdleRef.current();
        }
      } catch {
        failRef.current += 1;
        if (failRef.current >= 3) {
          onFailedRef.current();
        }
      } finally {
        inFlightRef.current = false;
      }
    };

    const timer = window.setInterval(() => {
      void tick();
    }, PROVISION_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [options.enabled, options.startedAtRef, options.lastKeyRef]);
}
