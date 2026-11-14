import { request } from '@/services/request';

const BASE = '/system/channel-config';

export interface FeishuConfig {
  enabled: boolean;
  app_id: string;
  app_secret: string;
  encrypt_key: string;
  verification_token: string;
  streaming_enabled: boolean;
}

export interface DingTalkConfig {
  enabled: boolean;
  client_id: string;
  client_secret: string;
  card_template_id: string;
  card_template_key: string;
  streaming_enabled: boolean;
}

export interface WecomConfig {
  enabled: boolean;
  bot_id: string;
  secret: string;
  streaming_enabled: boolean;
}

export interface WechatConfig {
  enabled: boolean;
  bot_token: string;
  streaming_enabled: boolean;
}

export type ChannelKey = 'feishu' | 'dingtalk' | 'wecom' | 'wechat';

export interface ChannelConfigResponse {
  feishu?: FeishuConfig;
  dingtalk?: DingTalkConfig;
  wecom?: WecomConfig;
  wechat?: WechatConfig;
}

export interface TestResult {
  success: boolean;
  message: string;
}

export type ChannelConfigPayload = Partial<
  FeishuConfig & DingTalkConfig & WecomConfig & WechatConfig
>;

/** QR-code login image + opaque polling token (wechat). */
export interface ChannelQrcodeResponse {
  qrcode_img: string;
  poll_token: string;
}

/** QR-code login status + credentials issued on success (wechat). */
export interface ChannelQrcodeStatusResponse {
  status: string;
  credentials: Record<string, string>;
}

function jsonOptions(method: 'POST' | 'PUT', body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

export const channelConfigApi = {
  get: () => request<ChannelConfigResponse>(`${BASE}/`),
  update: (channel: ChannelKey, payload: ChannelConfigPayload) =>
    request<{ channel: ChannelKey; config: Record<string, unknown> }>(
      `${BASE}/${channel}`,
      jsonOptions('PUT', payload),
    ),
  test: (channel: ChannelKey) => request<TestResult>(`${BASE}/${channel}/test`, { method: 'POST' }),
  getQrcode: (channel: ChannelKey) => request<ChannelQrcodeResponse>(`${BASE}/${channel}/qrcode`),
  getQrcodeStatus: (channel: ChannelKey, pollToken: string) =>
    request<ChannelQrcodeStatusResponse>(
      `${BASE}/${channel}/qrcode/status?token=${encodeURIComponent(pollToken)}`,
    ),
};
