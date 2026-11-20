import type { Role } from '@/auth';
import {
  getConfiguredRedirectOrigins,
  getConfiguredRedirectPaths,
  resolveAgentCenterUrl,
} from '@/auth';

/** Fallback when this deployment has no Host console. */
const NO_HOST_CONSOLE_FALLBACK = '/data-source';

/** Host console URL, or '' when none is reachable (treat '/' as missing to avoid a loop). */
export function getAgentCenterLanding(): string {
  const url = resolveAgentCenterUrl();
  return !url || url === '/' ? '' : url;
}

/** Default landing for non-platform users: Host console, else DataBridge. */
export function getDefaultLanding(): string {
  return getAgentCenterLanding() || NO_HOST_CONSOLE_FALLBACK;
}

export function getFirstAllowedPage(role: Role): string {
  if (role === 'platform_admin') return '/user-center/tenants';
  return getDefaultLanding();
}

function isAllowedRedirect(destination: URL): boolean {
  if (destination.origin === window.location.origin) return true;
  const allowedOrigins = getConfiguredRedirectOrigins();
  const allowedPaths = getConfiguredRedirectPaths();
  return (
    allowedOrigins.includes(destination.origin) &&
    allowedPaths.some(
      (path) =>
        path === '/' ||
        destination.pathname === path ||
        destination.pathname.startsWith(`${path.replace(/\/$/, '')}/`),
    )
  );
}

/**
 * Full-page navigation after a successful login/registration so the whole
 * app re-runs auth bootstrap with the fresh session cookie.
 */
export function redirectAfterAuth(target: string | null, role: Role): void {
  const fallbackUrl = new URL(getFirstAllowedPage(role), window.location.origin);

  let destination: URL;
  try {
    destination = new URL(target || fallbackUrl.toString(), window.location.origin);
  } catch {
    destination = fallbackUrl;
  }

  if (!['http:', 'https:'].includes(destination.protocol) || !isAllowedRedirect(destination)) {
    destination = fallbackUrl;
  }

  window.location.assign(destination.toString());
}
