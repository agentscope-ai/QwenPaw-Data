import { request, requestFile } from '@/services/request';
import { answerToolConfirmation } from './toolConfirmation';
import { getRuntimeSettings, updateRuntimeSettings } from './runtimeSettings';
import { parseSessionArtifacts } from '../model/artifacts/sessionArtifacts';
import type { LineDiffResult } from '../model/compare/lineDiff';
import type {
  ArtifactAccessPurpose,
  ArtifactComment,
  ArtifactShare,
  ActiveModels,
  AskUserQuestionToolResult,
  AttachmentRef,
  ComparisonResult,
  CompareRecommendationItem,
  ConsoleChat,
  ConsoleSession,
  DatasourceOption,
  DatasetOption,
  ModelProvider,
  PatchConsoleSessionRequest,
  ProviderModel,
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
  answerToolConfirmation,
  getModelProviders() {
    return request<{ providers: ModelProvider[] }>('/preferences/providers');
  },

  updateModelProvider(providerId: string, body: { api_key?: string; base_url?: string | null }) {
    return request<{ provider: ModelProvider }>(
      `/preferences/providers/${encodeURIComponent(providerId)}`,
      { method: 'PUT', body: JSON.stringify(body) },
    );
  },

  clearModelProvider(providerId: string) {
    return request<{ ok: boolean }>(`/preferences/providers/${encodeURIComponent(providerId)}`, {
      method: 'DELETE',
    });
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
      context_size?: number | null;
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

  getRuntimeSettings,
  updateRuntimeSettings,

  createSession(body: {
    title?: string | null; // Display title for the new session.
    datasource_id?: string | null; // Optional datasource for an analysis session.
    agent_id?: string; // Agent implementation; review creates an isolated review session.
    parent_session_id?: string | null; // Source session whose work the review agent reads.
    enable_clarification?: boolean; // false: skip ask-user questions for chats opened in this session.
  }) {
    return request<{ session: ConsoleSession }>('/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  listSessions(
    params: {
      session_id?: string;
      search_text?: string;
      status?: 'idle' | 'running';
      datasource_id?: string;
      channel?: string;
      sort?: 'updated_desc' | 'updated_asc' | 'chat_count_desc';
      page?: number;
      page_size?: number;
      min_chat_count?: number;
    } = {},
    signal?: AbortSignal,
  ) {
    return request<{ items: ConsoleSession[]; total: number }>(`/sessions${queryString(params)}`, {
      signal,
    });
  },

  getSession(sessionId: string) {
    return request<{ session: ConsoleSession }>(`/sessions/${sessionId}`);
  },

  async getSessionArtifacts(sessionId: string, signal?: AbortSignal) {
    const result = await request<unknown>(`/sessions/${encodeURIComponent(sessionId)}/artifacts`, {
      signal,
    });
    return parseSessionArtifacts(result, sessionId);
  },

  updateSession(sessionId: string, body: PatchConsoleSessionRequest) {
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

  forkSession(sessionId: string, chatId: string) {
    return request<{ session: ConsoleSession }>(`/sessions/${sessionId}/fork`, {
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId }),
    });
  },

  getSnapshot(sessionId: string, signal?: AbortSignal, beforeSequence?: number) {
    return request<SessionSnapshot>(
      `/sessions/${sessionId}/snapshot${queryString({ limit: 15, before_sequence: beforeSequence })}`,
      { signal },
    );
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

  uploadAttachment(sessionId: string, file: File) {
    const body = new FormData();
    body.append('session_id', sessionId);
    body.append('file', file);
    return request<{ attachment: AttachmentRef }>('/console/upload', {
      method: 'POST',
      body,
    });
  },

  deleteAttachment(attachmentId: string) {
    return request<void>(`/console/attachments/${encodeURIComponent(attachmentId)}`, {
      method: 'DELETE',
    });
  },

  createChat(body: {
    session_id: string;
    text: string;
    datasource_id?: string | null;
    dataset_names?: string[] | null;
    attachment_ids?: string[];
    artifact_comments?: ArtifactComment[];
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

  steer(sessionId: string, chatId: string, text: string, artifactComments: ArtifactComment[] = []) {
    return request<void>(`/sessions/${sessionId}/chats/${chatId}/steer`, {
      method: 'POST',
      body: JSON.stringify({ text, artifact_comments: artifactComments }),
    });
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
      `/sessions/${sessionId}/chats/${chatId}/clarification/answer`,
      {
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

  listDatasets(datasourceId: string, signal?: AbortSignal) {
    return request<{ items: DatasetOption[] }>(
      `/datasets${queryString({ datasource_id: datasourceId })}`,
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
    return request<void>(`/sessions/${sessionId}/chats/${chatId}/feedback/reaction`, {
      method: 'DELETE',
    });
  },

  getWorkspaceFile(sessionId: string, relativePath: string, purpose: ArtifactAccessPurpose) {
    return requestFile(
      `/sessions/${sessionId}/files/access${queryString({
        path: relativePath,
        purpose,
      })}`,
    );
  },

  /**
   * Generic two-document comparison over artifact-dir-relative paths. Pure
   * read: it returns a verdict without persisted review/evidence records and never
   * writes a file, so the compare config panel can compare any two documents
   * (a GT against a draft, or two drafts) without a report-reproduction run.
   */
  compareWorkspaceFiles(
    sessionId: string,
    body: {
      target_path: string; // Current file, relative to the artifact directory.
      reference_path: string; // Selected reference in the same artifact directory.
      tolerance?: number; // Relative numeric tolerance; backend default is 0.001.
    },
  ) {
    return request<ComparisonResult>(`/sessions/${sessionId}/files/compare`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /**
   * Generic line-by-line diff over artifact-dir-relative paths (vscode-style).
   * Pure read: returns aligned rows plus intra-line highlight spans and never
   * writes a file, so any two same-family documents can be compared.
   */
  diffWorkspaceFiles(sessionId: string, body: { target_path: string; reference_path: string }) {
    return request<LineDiffResult>(`/sessions/${sessionId}/files/diff`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /**
   * LLM compare recommendations for the picker: send the candidates and the
   * document being previewed, get back 0-3 valid same-family pairs. Degrades to
   * an empty list when no model is configured or the call fails.
   */
  fetchCompareRecommendations(
    sessionId: string,
    body: {
      current_path?: string | null;
      candidates: Array<{ path: string; name: string }>;
    },
  ) {
    return request<{ recommendations: CompareRecommendationItem[] }>(
      `/sessions/${sessionId}/files/compare/recommendations`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  },

  /**
   * Upload a local document into the session artifact dir (`uploads/`) so the
   * compare picker can pair it like any workspace file. Returns the
   * artifact-dir-relative path.
   */
  uploadWorkspaceFile(sessionId: string, file: File) {
    const body = new FormData();
    body.append('file', file);
    return request<{ path: string }>(`/sessions/${sessionId}/files/upload`, {
      method: 'POST',
      body,
    });
  },

  shareWorkspaceFile(sessionId: string, relativePath: string) {
    return request<ArtifactShare>(`/sessions/${sessionId}/files/share`, {
      method: 'POST',
      body: JSON.stringify({ path: relativePath }),
    });
  },

  reportBehaviorEvent(body: {
    kind: 'trace_view' | 'segment_toggle' | 'session_revisit';
    session_id: string;
    chat_id?: string | null;
    target_id?: string;
    payload?: Record<string, unknown>;
  }) {
    return request<{ ok: boolean }>('/console/events', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },
};
