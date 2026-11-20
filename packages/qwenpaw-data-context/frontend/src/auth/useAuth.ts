import { useMemo } from 'react';
import type { AuthState } from './types';
import { EMPTY_AUTH_STATE } from './types';

/**
 * Stub hook — the actual implementation lives in each app's authStore.
 * This provides a fallback for contexts where no store is available.
 */
export function useAuth(): AuthState {
  return useMemo(() => EMPTY_AUTH_STATE, []);
}
