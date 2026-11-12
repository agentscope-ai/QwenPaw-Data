import { request } from '@/services/request';

export type SettlementCardStatus = 'pending' | 'queried' | 'confirmed' | 'dismissed';

export type SettlementCardType =
  'metric_caliber' | 'dimension_def' | 'column_meaning' | 'dataset_usage';

export interface SettlementCard {
  id: string; // 沉淀卡片唯一标识。
  user_id: string; // 卡片所属用户 ID。
  tenant_id: string; // 卡片所属租户 ID。
  workspace_id: string; // 卡片所属工作空间 ID。
  session_id: string; // 卡片所属会话 ID，确认和忽略操作必须使用此值。
  source_chat_id: string; // 产生卡片的来源 Chat ID。
  type: SettlementCardType | string; // 语义卡片类型，兼容后端新增类型。
  fields: Record<string, string>; // 卡片字段；确认时传入的 fields 会完整替换这些字段。
  status: SettlementCardStatus | string; // 卡片处理状态，兼容后端新增状态。
  created_at?: string | null; // 创建时间。
  updated_at?: string | null; // 最后更新时间。
  confirmed_at?: string | null; // 确认时间，未确认时为空。
}

interface SettlementCardsResponse {
  cards: SettlementCard[]; // 当前查询范围内的卡片列表。
  count: number; // 列表条目总数。
}

interface SettlementCardActionResponse {
  ok: boolean; // 本次卡片状态操作是否成功，不代表异步沉淀写入已完成。
  card: SettlementCard; // 操作后的卡片。
}

function settlementCardsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/settlement/cards`;
}

export const settlementApi = {
  listWorkspaceCards() {
    // 全量读取当前身份的工作空间卡片，不传 status，避免漏掉 queried。
    return request<SettlementCardsResponse>('/settlement/cards');
  },

  listCards(sessionId: string, status?: SettlementCardStatus) {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    return request<SettlementCardsResponse>(`${settlementCardsPath(sessionId)}${query}`);
  },

  confirmCard(sessionId: string, cardId: string, fields?: Record<string, string> | null) {
    return request<SettlementCardActionResponse>(
      `${settlementCardsPath(sessionId)}/${encodeURIComponent(cardId)}/confirm`,
      {
        method: 'POST',
        body: JSON.stringify({ fields: fields ?? null }),
      },
    );
  },

  dismissCard(sessionId: string, cardId: string) {
    return request<SettlementCardActionResponse>(
      `${settlementCardsPath(sessionId)}/${encodeURIComponent(cardId)}/dismiss`,
      { method: 'POST' },
    );
  },
};
