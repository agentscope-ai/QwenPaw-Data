/** Silent background poll; the table reloads only when instance status changes. */
export const PROVISION_POLL_INTERVAL_MS = 8000;
export const PROVISION_POLL_MAX_MS = 5 * 60 * 1000;

export type ProvisionPollItem = {
  id: string;
  status?: string | null;
  instance_status?: string | null;
  onboarding_state?: string | null;
};

export function isInstanceProvisioning(instanceStatus?: string | null): boolean {
  return instanceStatus === 'queued' || instanceStatus === 'provisioning';
}

export function shouldPollProvisioning(items: ProvisionPollItem[]): boolean {
  return items.some((item) => isInstanceProvisioning(item.instance_status));
}

export function provisionStatusKey(items: ProvisionPollItem[]): string {
  return items
    .map((item) =>
      [item.id, item.status ?? '', item.instance_status ?? '', item.onboarding_state ?? ''].join(
        ':',
      ),
    )
    .sort()
    .join('|');
}
