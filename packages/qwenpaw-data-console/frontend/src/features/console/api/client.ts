import { request, requestFile } from '@/services/request';
import type {
  ArtifactAccessPurpose,
  ActiveModels,
  AskUserQuestionToolResult,
  AttachmentRef,
  ConsoleChat,
  ConsoleSession,
  DatasourceOption,
  ModelProvider,
  ProviderModel,
  RuntimeSettings,
  SessionSnapshot,
  TraceResponse,
} from './types';

function queryString(params: Record<string, string | number | null | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') {
      search.set(key, String(value));
    }
  });
  const value = search.toString();
  return value ? `?${value}` : '';
}

export const dataConsoleApi = {
  getModelProviders() {
    return request<{ providers: ModelProvider[] }>('/preferences/providers');
  },

  updateModelProvider(
    providerId: string,
    body: { api_key?: string; base_url?: string | null },
  ) {
    return request<{ provider: ModelProvider }>(
      `/preferences/providers/${encodeURIComponent(providerId)}`,
      { method: 'PUT', body: JSON.stringify(body) },
    );
  },

  clearModelProvider(providerId: string) {
    return request<{ ok: boolean }>(
      `/preferences/providers/${encodeURIComponent(providerId)}`,
      { method: 'DELETE' },
    );
  },

  getProviderModels(providerId: string) {
    return request<{ models: ProviderModel[] }>(
      `/preferences/providers/${encodeURIComponent(providerId)}/models`,
    );
  },

  upsertProviderModel(
    providerId: string,
    modelId: string,
    body: {
      source: 'extra' | 'override';
      name?: string | null;
      thinking_enabled?: boolean | null;
      generate_kwargs?: Record<string, unknown> | null;
    },
  ) {
    return request<{ model: ProviderModel }>(
      `/preferences/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}`,
      { method: 'PUT', body: JSON.stringify(body) },
    );
  },

  deleteProviderModel(providerId: string, modelId: string) {
    return request<{ ok: boolean }>(
      `/preferences/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}`,
      { method: 'DELETE' },
    );
  },

  getActiveModels() {
    return request<{ active_models: ActiveModels }>('/preferences/active-models');
  },

  updateActiveModels(body: {
    default_provider_id: string;
    default_model_id: string;
    light_provider_id: string | null;
    light_model_id: string | null;
  }) {
    return request<{ active_models: ActiveModels }>('/preferences/active-models', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  getRuntimeSettings() {
    return request<{ runtime_settings: RuntimeSettings }>(
      '/preferences/runtime-settings',
    );
  },

  updateRuntimeSettings(body: Partial<RuntimeSettings>) {
    return request<{ runtime_settings: RuntimeSettings }>(
      '/preferences/runtime-settings',
      { method: 'PUT', body: JSON.stringify(body) },
    );
  },

  createSession(body: {
    title?: string | null;
    datasource_id?: string | null;
  }) {
    return request<{ session: ConsoleSession }>('/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  listSessions(params: {
    search_text?: string;
    status?: 'idle' | 'running';
    datasource_id?: string;
    sort?: 'updated_desc' | 'updated_asc' | 'chat_count_desc';
    page?: number;
    page_size?: number;
  } = {}) {
    return request<{ items: ConsoleSession[]; total: number }>(
      `/sessions${queryString(params)}`,
    );
  },

  updateSession(sessionId: string, body: { title: string }) {
    return request<{ session: ConsoleSession }>(`/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  },

  deleteSession(sessionId: string) {
    return request<{ status: string }>(`/sessions/${sessionId}`, {
      method: 'DELETE',
    });
  },

  getSnapshot(sessionId: string) {
    return request<SessionSnapshot>(`/sessions/${sessionId}/snapshot`);
  },

  editPlan(
    sessionId: string,
    chatId: string,
    body: { plan: ConsoleChat['plan']; reason?: string | null },
  ) {
    return request<{ plan: ConsoleChat['plan'] }>(
      `/sessions/${sessionId}/chats/${chatId}/plan/edit`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  },

  createChat(body: {
    session_id: string;
    text: string;
    datasource_id: string;
    attachments?: AttachmentRef[] | null;
  }) {
    return request<{ chat: ConsoleChat }>('/console/chat', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  stopChat(sessionId: string, chatId: string) {
    return request<{ chat: ConsoleChat }>(
      `/console/chat/stop${queryString({ session_id: sessionId, chat_id: chatId })}`,
      { method: 'POST' },
    );
  },

  steer(sessionId: string, chatId: string, text: string) {
    return request<void>(
      `/sessions/${sessionId}/chats/${chatId}/steer`,
      { method: 'POST', body: JSON.stringify({ text }) },
    );
  },

  answerClarification(
    sessionId: string,
    chatId: string,
    body: {
      clarification_id: string;
      result: AskUserQuestionToolResult;
    },
  ) {
    return request<{ chat: ConsoleChat }>(
      `/sessions/${sessionId}/chats/${chatId}/clarification/answer`, {
        method: 'POST',
        body: JSON.stringify(body),
      },
    );
  },

  getTrace(
    sessionId: string,
    chatId: string,
    view: 'raw' | 'business' | 'both',
    signal?: AbortSignal,
  ) {
    return request<TraceResponse>(
      `/sessions/${sessionId}/chats/${chatId}/trace${queryString({ view })}`,
      { signal },
    );
  },

  listDatasources() {
    return request<{ items: DatasourceOption[] }>('/datasources');
  },

  createFeedback(
    sessionId: string,
    chatId: string,
    body: {
      kind: 'like' | 'dislike' | 'copy' | 'artifact_comment';
      reason?: string | null;
      detail?: string | null;
      artifact_ref?: {
        artifact_id: string;
        line_start: number;
        line_end: number;
        selected_text?: string | null;
      } | null;
    },
  ) {
    return request<{ feedback: { id: string } }>(
      `/sessions/${sessionId}/chats/${chatId}/feedback`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  },

  getFeedbackReaction(sessionId: string, chatId: string) {
    return request<{
      feedback: { id: string; kind: 'like' | 'dislike' } | null;
    }>(`/sessions/${sessionId}/chats/${chatId}/feedback/reaction`);
  },

  clearFeedbackReaction(sessionId: string, chatId: string) {
    return request<void>(
      `/sessions/${sessionId}/chats/${chatId}/feedback/reaction`,
      { method: 'DELETE' },
    );
  },

  getWorkspaceFile(
    sessionId: string,
    relativePath: string,
    purpose: ArtifactAccessPurpose,
  ) {
    return requestFile(
      `/sessions/${sessionId}/files/access${queryString({
        path: relativePath,
        purpose,
      })}`,
    );
  },
};
