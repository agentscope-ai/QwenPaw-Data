import { request } from '@/services/request';

export type SkillDraftStatus =
  'generating' | 'ready' | 'revising' | 'accepted' | 'dismissed' | 'failed';

export interface SkillDraft {
  id: string;
  session_id: string;
  source_chat_id: string;
  status: SkillDraftStatus;
  name: string;
  files?: Record<string, string>;
  created_at: string;
  updated_at: string;
}

function draftsPath(sessionId: string, draftId?: string): string {
  const root = `/sessions/${encodeURIComponent(sessionId)}/skill-drafts`;
  return draftId ? `${root}/${encodeURIComponent(draftId)}` : root;
}

export const skillDraftsApi = {
  list(sessionId: string) {
    return request<{ skill_drafts: SkillDraft[]; count: number }>(draftsPath(sessionId));
  },

  create(sessionId: string, sourceChatId: string) {
    return request<{ skill_draft: SkillDraft }>(draftsPath(sessionId), {
      method: 'POST',
      body: JSON.stringify({ source_chat_id: sourceChatId }),
    });
  },

  get(sessionId: string, draftId: string) {
    return request<{ skill_draft: SkillDraft }>(draftsPath(sessionId, draftId));
  },

  revise(sessionId: string, draftId: string, text: string) {
    return request<{ skill_draft: SkillDraft }>(`${draftsPath(sessionId, draftId)}/revise`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  },

  confirm(sessionId: string, draftId: string) {
    return request<{ created: string; skill_draft: SkillDraft }>(
      `${draftsPath(sessionId, draftId)}/confirm`,
      { method: 'POST' },
    );
  },

  dismiss(sessionId: string, draftId: string) {
    return request<{ skill_draft: SkillDraft }>(`${draftsPath(sessionId, draftId)}/dismiss`, {
      method: 'POST',
    });
  },
};
