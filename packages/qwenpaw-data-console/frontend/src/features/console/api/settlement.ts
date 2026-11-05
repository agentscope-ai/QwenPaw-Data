import { request } from '@/services/request';

export type SettlementCardStatus =
  | 'pending'
  | 'queried'
  | 'confirmed'
  | 'dismissed';

export type SettlementCardType =
  | 'metric_caliber'
  | 'dimension_def'
  | 'column_meaning'
  | 'dataset_usage';

export interface SettlementCard {
  id: string;
  session_id: string;
  source_chat_id: string;
  type: SettlementCardType | string;
  fields: Record<string, string>;
  status: SettlementCardStatus | string;
  created_at?: string | null;
  updated_at?: string | null;
  confirmed_at?: string | null;
}

function settlementCardsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/settlement/cards`;
}

export const settlementApi = {
  listCards(sessionId: string, status?: SettlementCardStatus) {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    return request<{ cards: SettlementCard[]; count: number }>(
      `${settlementCardsPath(sessionId)}${query}`,
    );
  },

  confirmCard(
    sessionId: string,
    cardId: string,
    fields?: Record<string, string> | null,
  ) {
    return request<{ ok: boolean; card: SettlementCard }>(
      `${settlementCardsPath(sessionId)}/${encodeURIComponent(cardId)}/confirm`,
      {
        method: 'POST',
        body: JSON.stringify({ fields: fields ?? null }),
      },
    );
  },

  dismissCard(sessionId: string, cardId: string) {
    return request<{ ok: boolean; card: SettlementCard }>(
      `${settlementCardsPath(sessionId)}/${encodeURIComponent(cardId)}/dismiss`,
      { method: 'POST' },
    );
  },
};
