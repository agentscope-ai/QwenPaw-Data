const externalChannels = ['feishu', 'dingtalk', 'wecom', 'wechat'] as const;
type ChannelStatus = Partial<Record<(typeof externalChannels)[number], { enabled: boolean }>>;

/** A failed or absent IM configuration must fail closed only for IM actions. */
export function cronChannelAvailability(config?: ChannelStatus) {
  const enabled = new Map<string, boolean>([['console', true]]);
  for (const channel of externalChannels) enabled.set(channel, config?.[channel]?.enabled === true);
  return enabled;
}
