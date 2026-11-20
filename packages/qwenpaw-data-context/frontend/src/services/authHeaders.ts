/**
 * Cookie auto-carries credentials, so standalone mode adds no headers. The
 * packaged console (portal auth mode) marks proxied requests instead.
 */
import { getConsoleConfig, isConsolePortal } from './consoleConfig';

export function buildAuthHeaders(): Record<string, string> {
  // Packaged console: the local portal authenticates proxied requests by this marker.
  if (isConsolePortal() || getConsoleConfig()?.auth_mode === 'portal') return { 'X-QwenPaw-Console': '1' };
  return {};
}
