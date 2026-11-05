export function buildNewConsoleSessionPath(): string {
  return '/console';
}

export function buildAllConsoleSessionsPath(): string {
  return '/console/sessions';
}

export function getSessionCatalogReloadKey(
  pathname: string,
  navigationKey: string,
): string | null {
  return pathname === buildAllConsoleSessionsPath() ? navigationKey : null;
}

export function buildSkillsHubPath(): string {
  return '/console/skills';
}

export function buildMcpHubPath(): string {
  return '/console/mcp';
}

export function buildCronJobsPath(): string {
  return '/console/cron-jobs';
}

export function buildAgentConfigurationPath(): string {
  return '/console/settings/agent';
}

export function buildChannelConfigurationPath(): string {
  return '/console/settings/channels';
}

export function buildConsoleSessionPath(sessionId: string): string {
  const search = new URLSearchParams({ session_id: sessionId });
  return `/console?${search.toString()}`;
}
