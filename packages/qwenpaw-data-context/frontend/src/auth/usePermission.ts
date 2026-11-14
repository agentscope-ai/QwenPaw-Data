import { useMemo } from 'react';
import type { Resource, Role } from './types';
import { checkPermission } from './permissions';

/**
 * Check whether the current user has a given permission.
 * UI-only — does not replace backend checks.
 */
export function usePermission(role: Role, resource: Resource): boolean {
  return useMemo(() => checkPermission(role, resource), [role, resource]);
}
