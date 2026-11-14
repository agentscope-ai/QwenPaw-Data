export function buildNewConsoleSessionPath(): string {
  return '/console';
}

export function buildAllConsoleSessionsPath(): string {
  return '/console/sessions';
}

export function buildSkillsHubPath(): string {
  return '/console/skills';
}

export function buildCronJobsPath(): string {
  return '/console/cron-jobs';
}

export function buildSettlementCenterPath(): string {
  return '/console/settlement';
}

export function buildAgentConfigurationPath(returnTo?: string): string {
  const base = '/console/settings/agent';
  return returnTo && isConsoleReturnPath(returnTo)
    ? `${base}?${new URLSearchParams({ return_to: returnTo })}`
    : base;
}

function isConsoleReturnPath(value: string): boolean {
  return value === '/console' || value.startsWith('/console?');
}

export function getAgentSettingsReturnPath(search: string): string | null {
  const value = new URLSearchParams(search).get('return_to');
  return value && isConsoleReturnPath(value) ? value : null;
}

export function buildChannelConfigurationPath(): string {
  return '/console/settings/channels';
}

export function buildMcpConfigurationPath(): string {
  return '/console/settings/mcp';
}

export function buildUsagePath(): string {
  return '/console/settings/usage';
}

export function buildConsoleSessionPath(sessionId: string): string {
  const search = new URLSearchParams({ session_id: sessionId });
  return `/console?${search.toString()}`;
}
