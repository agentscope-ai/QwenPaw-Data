import type {
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISession,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type {
  IContent,
  IAgentScopeRuntimeMessage,
  IAgentScopeRuntimeResponse,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiRequestError } from '@/services/request';
import { dataConsoleApi } from '../api/client';
import { subscribeChatEvents } from '../api/events';
import type {
  AskUserQuestionToolResult,
  ConsoleChat,
  ConsoleSession,
  ConsoleStreamEvent,
  DatasourceOption,
  Plan,
} from '../api/types';
import { createDraftQueueSessionId } from './draftQueueSessionId';
import { selectPendingAskUserQuestion } from './askUserQuestion';
import {
  createStreamSettlementGuard,
  isSnapshotSessionRunning,
  isTerminalChatStatus,
  selectChatStreamCompletionAction,
} from './chatStreamLifecycle';
import {
  createChatStreamProjection,
  projectStreamEvent,
  resolveChatPlan,
  withChatErrorMessage,
  type ChatStreamProjection,
} from './projectStream';
import {
  selectChatTimeline,
  type ChatTimelineItem,
} from './selectChatTimeline';
import { loadAllConsoleSessions } from './loadAllConsoleSessions';
import {
  createBizEventRuntimeMessage,
  createSegmentRuntimeMessage,
} from './runtimeProjection';
import {
  createRuntimeStreamGuardMessage,
  shouldForwardRuntimeStreamEvent,
} from './runtimeSteer';

export type ConsoleConnectionStatus =
  | 'loading'
  | 'idle'
  | 'connecting'
  | 'streaming'
  | 'eof'
  | 'aborted'
  | 'error';

export interface ConsoleChatController {
  session: ConsoleSession | null;
  inputQueueSessionId: string;
  sessions: ConsoleSession[];
  projections: ChatStreamProjection[];
  currentChatId: string | null;
  currentProjection: ChatStreamProjection | null;
  currentTimeline: ChatTimelineItem[];
  datasources: DatasourceOption[];
  selectedDatasourceId: string | null;
  runtimeSession: IAgentScopeRuntimeWebUISession | null;
  connectionStatus: ConsoleConnectionStatus;
  error: string | null;
  modelConfigurationRequired: boolean;
  selectChat: (chatId: string) => void;
  selectDatasource: (datasourceId: string) => void;
  startNewSession: () => void;
  reloadSessions: () => void;
  renameSession: (sessionId: string, title: string) => Promise<ConsoleSession>;
  deleteSession: (sessionId: string) => Promise<void>;
  submitFeedback: (
    chatId: string,
    kind: 'like' | 'dislike' | 'copy',
    detail?: { reason?: string | null; detail?: string | null },
  ) => Promise<void>;
  getFeedbackReaction: (chatId: string) => Promise<'like' | 'dislike' | null>;
  clearFeedbackReaction: (chatId: string) => Promise<void>;
  steerCurrentChat: (text: string) => Promise<void>;
  submitRuntimeRequest: (
    text: string,
    signal?: AbortSignal,
  ) => Promise<Response>;
  isCurrentChatRunning: () => Promise<boolean>;
  answerClarification: (
    chatId: string,
    clarificationId: string,
    result: AskUserQuestionToolResult,
  ) => Promise<void>;
  editPlan: (
    chatId: string,
    plan: Plan,
    reason?: string | null,
  ) => Promise<void>;
  loadRawTrace: (chatId: string) => Promise<void>;
  stopCurrentChat: () => void;
}

interface ConsoleState {
  session: ConsoleSession | null;
  projections: ChatStreamProjection[];
  currentChatId: string | null;
}

function getRuntimeStatus(chat: ConsoleChat): IAgentScopeRuntimeResponse['status'] {
  if (chat.status === 'completed') return AgentScopeRuntimeRunStatus.Completed;
  if (chat.status === 'failed') return AgentScopeRuntimeRunStatus.Failed;
  if (chat.status === 'canceled') return AgentScopeRuntimeRunStatus.Canceled;
  if (chat.status === 'created') return AgentScopeRuntimeRunStatus.Created;
  return AgentScopeRuntimeRunStatus.InProgress;
}

function getRuntimeResponseStatus(
  status: ChatStreamProjection['responseStatus'],
): AgentScopeRuntimeRunStatus {
  if (status === 'completed') return AgentScopeRuntimeRunStatus.Completed;
  if (status === 'failed') return AgentScopeRuntimeRunStatus.Failed;
  if (status === 'cancelled') return AgentScopeRuntimeRunStatus.Canceled;
  if (status === 'created') return AgentScopeRuntimeRunStatus.Created;
  return AgentScopeRuntimeRunStatus.InProgress;
}

function requestCard(chat: ConsoleChat): IAgentScopeRuntimeWebUIMessage {
  return {
    id: `request-${chat.id}`,
    role: 'user',
    msgStatus: 'finished',
    cards: [
      {
        code: 'AgentScopeRuntimeRequestCard',
        data: {
          input: [
            {
              role: 'user',
              type: 'message',
              content: [{
                type: 'text',
                text: chat.user_input,
                status: AgentScopeRuntimeRunStatus.Completed,
              }],
            },
          ],
        },
      },
    ],
  };
}

export function timelineItemToRuntimeMessage(
  item: Exclude<ChatTimelineItem, { kind: 'user' }>,
  terminal: boolean,
): IAgentScopeRuntimeMessage {
  if (item.kind === 'raw') {
    const message = item.message;
    const messageStatus = getRuntimeResponseStatus(message.status);
    const runtimeMessage: IAgentScopeRuntimeMessage = {
      ...message,
      role: message.role ?? 'assistant',
      type: message.type as AgentScopeRuntimeMessageType,
      status: terminal && messageStatus === AgentScopeRuntimeRunStatus.InProgress
        ? AgentScopeRuntimeRunStatus.Completed
        : messageStatus,
      content: message.content.map((part) => ({
        ...part,
        status: terminal
          ? AgentScopeRuntimeRunStatus.Completed
          : getRuntimeResponseStatus(
          part.status === 'completed'
            ? 'completed'
            : part.status === 'failed'
              ? 'failed'
              : 'in_progress',
          ),
      })) as IContent[],
    };
    if (message.type === 'error') {
      return {
        ...runtimeMessage,
        type: AgentScopeRuntimeMessageType.ERROR,
        code: typeof message.metadata?.code === 'string' ? message.metadata.code : undefined,
        message: typeof message.metadata?.message === 'string'
          ? message.metadata.message
          : undefined,
      };
    }
    return runtimeMessage;
  }

  if (item.kind === 'biz_event') {
    return createBizEventRuntimeMessage(item.event);
  }

  return createSegmentRuntimeMessage(item.segment);
}

function responseCard(
  projection: ChatStreamProjection,
): IAgentScopeRuntimeWebUIMessage {
  const { chat } = projection;
  const terminal = isTerminalChatStatus(chat.status);
  const status = terminal
    ? getRuntimeStatus(chat)
    : projection.responseStatus
      ? getRuntimeResponseStatus(projection.responseStatus)
      : getRuntimeStatus(chat);
  const output = selectChatTimeline(projection)
    .filter((item): item is Exclude<ChatTimelineItem, { kind: 'user' }> => (
      item.kind !== 'user'
      && !(item.kind === 'biz_event' && item.event.presentation?.card_type === 'user')
    ))
    .map((item) => timelineItemToRuntimeMessage(item, terminal));

  return {
    id: `assistant-${chat.id}`,
    role: 'assistant',
    msgStatus: status === AgentScopeRuntimeRunStatus.Completed
      ? 'finished'
      : status === AgentScopeRuntimeRunStatus.Failed
        ? 'error'
        : status === AgentScopeRuntimeRunStatus.Canceled
          ? 'interrupted'
          : 'generating',
    cards: [
      {
        code: 'AgentScopeRuntimeResponseCard',
        data: {
          id: projection.responseId ?? `response-${chat.id}`,
          object: 'response',
          status,
          created_at: chat.started_at ? Date.parse(chat.started_at) : Date.now(),
          completed_at: chat.completed_at ? Date.parse(chat.completed_at) : undefined,
          output,
          error: chat.error ?? undefined,
        } satisfies IAgentScopeRuntimeResponse,
      },
    ],
  };
}

function buildRuntimeSession(
  session: ConsoleSession,
  projections: ChatStreamProjection[],
): IAgentScopeRuntimeWebUISession {
  const messages = [...projections]
    .sort((left, right) => left.chat.sequence - right.chat.sequence)
    .flatMap((projection) => [
      requestCard(projection.chat),
      responseCard(projection),
    ]);

  return {
    id: session.id,
    name: session.title,
    messages,
    generating: projections.some(({ chat }) => chat.status === 'running'),
  };
}

const draftRuntimeSession: IAgentScopeRuntimeWebUISession = {
  id: 'new',
  name: '新会话',
  messages: [],
  generating: false,
};

function eventToRuntimeChunk(
  event: ConsoleStreamEvent,
): IAgentScopeRuntimeResponse | IAgentScopeRuntimeMessage | IContent {
  if (event.object === 'response') {
    const terminal = ['completed', 'failed', 'cancelled'].includes(event.status);
    return {
      id: event.id,
      object: 'response',
      status: getRuntimeResponseStatus(event.status),
      created_at: Date.now(),
      completed_at: terminal
        ? Date.now()
        : undefined,
      output: terminal ? [] : [createRuntimeStreamGuardMessage(event.id)],
      usage: event.usage ?? undefined,
      error: event.error
        ? { code: event.error.code, message: event.error.message }
        : undefined,
    };
  }
  if (event.object === 'message') {
    return {
      ...event,
      role: event.role ?? 'assistant',
      type: event.type as AgentScopeRuntimeMessageType,
      status: getRuntimeResponseStatus(event.status),
      content: event.content.map((content) => ({
        ...content,
        status: content.status === 'completed'
          ? AgentScopeRuntimeRunStatus.Completed
          : content.status === 'failed'
            ? AgentScopeRuntimeRunStatus.Failed
            : AgentScopeRuntimeRunStatus.InProgress,
      })) as IContent[],
    };
  }
  if (event.object === 'content') {
    return {
      ...event,
      status: event.status === 'completed'
        ? AgentScopeRuntimeRunStatus.Completed
        : event.status === 'failed'
          ? AgentScopeRuntimeRunStatus.Failed
          : AgentScopeRuntimeRunStatus.InProgress,
    } as unknown as IContent;
  }
  if (event.object === 'error') {
    return {
      id: `error-${event.sequence_number}`,
      object: 'message',
      role: 'assistant',
      type: AgentScopeRuntimeMessageType.ERROR,
      content: [],
      status: AgentScopeRuntimeRunStatus.Failed,
      code: event.code,
      message: event.message,
    };
  }
  return {
    id: `projection-${event.chat_id}-${event.sequence_number}`,
    object: 'message',
    role: 'assistant',
    type: AgentScopeRuntimeMessageType.HEARTBEAT,
    content: [],
    status: AgentScopeRuntimeRunStatus.InProgress,
  };
}

export function parseRuntimeStreamChunk(chunk: string) {
  return eventToRuntimeChunk(JSON.parse(chunk) as ConsoleStreamEvent);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isModelConfigurationError(error: unknown): boolean {
  if (!(error instanceof ApiRequestError) || error.code !== 'VALIDATION') return false;
  return [
    'active models are not configured',
    'provider is not configured',
    'model is not available',
  ].some((message) => error.message.includes(message));
}

export function useConsoleChat(
  requestedSessionId?: string | null,
  onSessionResolved?: (session: ConsoleSession) => void,
  sessionCatalogReloadKey?: string | null,
): ConsoleChatController {
  const [state, setState] = useState<ConsoleState>({
    session: null,
    projections: [],
    currentChatId: null,
  });
  const [draftQueueSessionId, setDraftQueueSessionId] = useState(
    createDraftQueueSessionId,
  );
  const [datasources, setDatasources] = useState<DatasourceOption[]>([]);
  const [sessions, setSessions] = useState<ConsoleSession[]>([]);
  const [selectedDatasourceId, setSelectedDatasourceId] = useState<string | null>(null);
  const [sessionCatalogVersion, setSessionCatalogVersion] = useState(0);
  const [subscriptionVersion, setSubscriptionVersion] = useState(0);
  const [connectionStatus, setConnectionStatus] =
    useState<ConsoleConnectionStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const [modelConfigurationRequired, setModelConfigurationRequired] = useState(false);
  const stateRef = useRef(state);
  const selectedDatasourceIdRef = useRef(selectedDatasourceId);
  const activeSubscriptionRef = useRef<AbortController | null>(null);
  const activeRuntimeStreamsRef = useRef(new Set<string>());
  const userAbortedChatIdsRef = useRef(new Set<string>());
  const runtimeStreamAbortRef = useRef<AbortController | null>(null);
  const onSessionResolvedRef = useRef(onSessionResolved);
  const hasLoadedSessionCatalogRef = useRef(false);
  onSessionResolvedRef.current = onSessionResolved;

  useEffect(() => {
    const controller = new AbortController();

    void loadAllConsoleSessions(dataConsoleApi.listSessions)
      .then((items) => {
        if (controller.signal.aborted) return;
        hasLoadedSessionCatalogRef.current = true;
        setSessions(items);
      })
      .catch((catalogError) => {
        if (controller.signal.aborted || hasLoadedSessionCatalogRef.current) return;
        setError(`会话列表加载失败：${errorMessage(catalogError)}`);
      });

    return () => controller.abort();
  }, [sessionCatalogReloadKey, sessionCatalogVersion]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    selectedDatasourceIdRef.current = selectedDatasourceId;
  }, [selectedDatasourceId]);

  const replaceChat = useCallback((chat: ConsoleChat) => {
    const current = stateRef.current;
    const existing = current.projections.find(({ chat: item }) => item.id === chat.id);
    const nextProjection = existing
      ? {
          ...existing,
          chat: {
            ...chat,
            plan: resolveChatPlan(chat, existing.messages) ?? existing.chat.plan,
          },
          lastSequenceNumber: chat.last_sequence_number,
          messages: withChatErrorMessage(
            existing.messages,
            chat.id,
            Math.max(chat.last_sequence_number, 0),
            chat.error,
          ),
        }
      : createChatStreamProjection(chat);
    const projections = existing
      ? current.projections.map((projection) => (
          projection.chat.id === chat.id ? nextProjection : projection
        ))
      : [...current.projections, nextProjection];
    const hasRunningChat = projections.some(({ chat: item }) => item.status === 'running');
    const next = {
      session: current.session
        ? {
            ...current.session,
            status: hasRunningChat ? 'running' as const : 'idle' as const,
            chat_count: Math.max(current.session.chat_count, projections.length),
          }
        : null,
      projections,
      currentChatId: chat.id,
    };
    stateRef.current = next;
    setState(next);
    if (next.session) {
      setSessions((current) => [
        next.session as ConsoleSession,
        ...current.filter((session) => session.id !== next.session?.id),
      ]);
    }
  }, []);

  const applyEvent = useCallback((event: ConsoleStreamEvent) => {
    const current = stateRef.current;
    const projections = current.projections.map((projection) => (
      projection.chat.id === event.chat_id
        ? projectStreamEvent(projection, event)
        : projection
    ));
    const hasRunningChat = projections.some(({ chat }) => chat.status === 'running');
    const next: ConsoleState = {
      ...current,
      projections,
      session: current.session
        ? { ...current.session, status: hasRunningChat ? 'running' : 'idle' }
        : null,
    };
    stateRef.current = next;
    setState(next);
    setConnectionStatus('streaming');
  }, []);

  const selectedChatStatus = state.projections
    .find(({ chat }) => chat.id === state.currentChatId)?.chat.status;

  const createRuntimeSession = useCallback(async (
    title: string,
  ): Promise<ConsoleSession> => {
    const existing = stateRef.current.session;
    if (existing) return existing;

    const datasourceId = selectedDatasourceIdRef.current;
    if (!datasourceId) throw new Error('请先选择数据源');

    const created = await dataConsoleApi.createSession({
      title: title.trim().slice(0, 48) || '新会话',
      datasource_id: datasourceId,
    });
    const session = created.session;
    const next: ConsoleState = {
      session,
      projections: [],
      currentChatId: null,
    };
    stateRef.current = next;
    setState(next);
    setSessions((current) => [
      session,
      ...current.filter((item) => item.id !== session.id),
    ]);
    onSessionResolvedRef.current?.(session);
    return session;
  }, []);

  useEffect(() => {
    if (
      requestedSessionId
      && requestedSessionId === stateRef.current.session?.id
    ) {
      return;
    }

    const controller = new AbortController();

    const load = async () => {
      setConnectionStatus('loading');
      setError(null);

      try {
        const datasourceResponse = await dataConsoleApi.listDatasources();
        if (controller.signal.aborted) return;
        setDatasources(datasourceResponse.items);

        const datasourceId = datasourceResponse.items.find((item) => item.recommended)?.id
          ?? datasourceResponse.items[0]?.id
          ?? null;
        selectedDatasourceIdRef.current = datasourceId;
        setSelectedDatasourceId(datasourceId);

        if (!requestedSessionId) {
          const next: ConsoleState = {
            session: null,
            projections: [],
            currentChatId: null,
          };
          stateRef.current = next;
          setState(next);
          setConnectionStatus('idle');
          return;
        }

        const snapshot = await dataConsoleApi.getSnapshot(requestedSessionId);
        if (controller.signal.aborted) return;

        const projections = await Promise.all(snapshot.chats.map(async (chat) => {
          if (chat.plan?.tasks.length || chat.status !== 'running') {
            return createChatStreamProjection(chat);
          }
          try {
            const trace = await dataConsoleApi.getTrace(
              requestedSessionId,
              chat.id,
              'raw',
              controller.signal,
            );
            return createChatStreamProjection(chat, trace.messages);
          } catch {
            return createChatStreamProjection(chat);
          }
        }));
        const latestChat = [...snapshot.chats]
          .sort((left, right) => right.sequence - left.sequence)[0];
        const next = {
          session: snapshot.session,
          projections,
          currentChatId: latestChat?.id ?? null,
        };
        stateRef.current = next;
        setState(next);
        const sessionDatasourceId = snapshot.session.datasource_id ?? datasourceId;
        selectedDatasourceIdRef.current = sessionDatasourceId;
        setSelectedDatasourceId(sessionDatasourceId);
        setConnectionStatus('idle');
      } catch (loadError) {
        if (controller.signal.aborted) return;
        setError(errorMessage(loadError));
        setConnectionStatus('error');
      }
    };

    void load();
    return () => controller.abort();
  }, [requestedSessionId]);

  useEffect(() => {
    activeSubscriptionRef.current?.abort();
    activeSubscriptionRef.current = null;

    const projection = stateRef.current.projections
      .find(({ chat }) => chat.id === state.currentChatId);
    if (!projection) return;
    if (projection.chat.status !== 'running') {
      if (isTerminalChatStatus(projection.chat.status)) {
        setConnectionStatus(
          userAbortedChatIdsRef.current.has(projection.chat.id) ? 'aborted' : 'eof',
        );
      }
      return;
    }
    if (activeRuntimeStreamsRef.current.has(projection.chat.id)) return;

    const controller = new AbortController();
    activeSubscriptionRef.current = controller;
    setConnectionStatus('connecting');
    setError(null);
    setModelConfigurationRequired(false);

    void subscribeChatEvents({
      sessionId: projection.chat.session_id,
      chatId: projection.chat.id,
      afterSequenceNumber: projection.lastSequenceNumber,
      signal: controller.signal,
      onEvent: applyEvent,
    }).then((result) => {
      const latest = stateRef.current.projections
        .find(({ chat }) => chat.id === projection.chat.id);
      const action = selectChatStreamCompletionAction(
        result.reason,
        latest?.chat.status ?? null,
      );
      if (action === 'aborted') return;
      if (action === 'terminal') {
        setConnectionStatus('eof');
      } else if (action === 'reconnect') {
        setConnectionStatus('connecting');
        setSubscriptionVersion((version) => version + 1);
      } else {
        setError('SSE 在 Chat 仍运行时意外结束，请刷新 Snapshot 后重试。');
        setConnectionStatus('error');
      }
    }).catch((streamError) => {
      if (controller.signal.aborted) return;
      setError(errorMessage(streamError));
      setConnectionStatus('error');
    });

    return () => {
      controller.abort();
      if (activeSubscriptionRef.current === controller) {
        activeSubscriptionRef.current = null;
      }
    };
  }, [
    applyEvent,
    state.currentChatId,
    selectedChatStatus,
    subscriptionVersion,
  ]);

  const submitRuntimeRequest = useCallback(async (
    text: string,
    signal?: AbortSignal,
  ): Promise<Response> => {
    setConnectionStatus('connecting');
    const session = stateRef.current.session ?? await createRuntimeSession(text);
    const datasourceId = selectedDatasourceIdRef.current;
    if (!datasourceId) throw new Error('请先选择数据源');

    let chat: ConsoleChat;
    try {
      ({ chat } = await dataConsoleApi.createChat({
        session_id: session.id,
        text,
        datasource_id: datasourceId,
        attachments: null,
      }));
    } catch (requestError) {
      const requiresModelConfiguration = isModelConfigurationError(requestError);
      setModelConfigurationRequired(requiresModelConfiguration);
      setError(requiresModelConfiguration
        ? '尚未配置可用模型，请先完成 Provider 和激活模型配置。'
        : errorMessage(requestError));
      setConnectionStatus('error');
      throw requestError;
    }
    const streamController = new AbortController();
    runtimeStreamAbortRef.current?.abort();
    runtimeStreamAbortRef.current = streamController;
    activeRuntimeStreamsRef.current.add(chat.id);
    replaceChat(chat);
    setConnectionStatus('connecting');
    setError(null);
    setModelConfigurationRequired(false);

    const forwardAbort = () => streamController.abort();
    signal?.addEventListener('abort', forwardAbort, { once: true });
    const encoder = new TextEncoder();
    const steerMessageIds = new Set<string>();

    let cancelRuntimeBridge = () => streamController.abort();
    return new Response(new ReadableStream<Uint8Array>({
      start: (responseStream) => {
        const release = () => {
          activeRuntimeStreamsRef.current.delete(chat.id);
          signal?.removeEventListener('abort', forwardAbort);
        };
        const settlement = createStreamSettlementGuard(release);
        cancelRuntimeBridge = () => {
          if (settlement.settle()) setConnectionStatus('aborted');
          streamController.abort();
        };
        const close = (status: ConsoleConnectionStatus) => {
          if (!settlement.settle()) return;
          setConnectionStatus(status);
          responseStream.close();
        };
        const fail = (streamError: unknown) => {
          if (!settlement.settle()) return;
          setError(errorMessage(streamError));
          setConnectionStatus('error');
          responseStream.error(streamError);
        };
        const subscribe = () => {
          const latest = stateRef.current.projections
            .find(({ chat: item }) => item.id === chat.id);
          const afterSequenceNumber = latest?.lastSequenceNumber
            ?? chat.last_sequence_number;
          void subscribeChatEvents({
            sessionId: chat.session_id,
            chatId: chat.id,
            afterSequenceNumber,
            signal: streamController.signal,
            onEvent: (event) => {
              if (settlement.isSettled()) return;
              applyEvent(event);
              if (shouldForwardRuntimeStreamEvent(event, steerMessageIds)) {
                responseStream.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
              }
            },
          }).then((result) => {
            const current = stateRef.current.projections
              .find(({ chat: item }) => item.id === chat.id);
            const action = selectChatStreamCompletionAction(
              result.reason,
              current?.chat.status ?? null,
            );
            if (action === 'aborted') {
              close('aborted');
            } else if (action === 'terminal') {
              close('eof');
            } else if (action === 'reconnect') {
              setConnectionStatus('connecting');
              queueMicrotask(() => {
                if (!settlement.isSettled()) subscribe();
              });
            } else {
              fail(new Error('SSE 在 Chat 非运行态时意外结束，请刷新 Snapshot 后重试。'));
            }
          }).catch((streamError) => {
            if (streamController.signal.aborted) {
              close('aborted');
              return;
            }
            fail(streamError);
          });
        };

        subscribe();
      },
      cancel: () => {
        cancelRuntimeBridge();
      },
    }), {
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
    });
  }, [applyEvent, createRuntimeSession, replaceChat]);

  const isCurrentChatRunning = useCallback(async (): Promise<boolean> => {
    const current = stateRef.current;
    const session = current.session;
    const chatId = current.currentChatId;
    const projection = current.projections.find(({ chat }) => chat.id === chatId);
    if (!session || !chatId || !projection) return false;

    try {
      const snapshot = await dataConsoleApi.getSnapshot(session.id);
      const refreshedChat = snapshot.chats.find((chat) => chat.id === chatId);
      const snapshotRunning = isSnapshotSessionRunning(snapshot);

      // Ignore a late poll after the user has already switched sessions/chats.
      const latest = stateRef.current;
      if (latest.session?.id !== session.id || latest.currentChatId !== chatId) {
        return latest.projections.some(({ chat }) => (
          chat.id === latest.currentChatId && chat.status === 'running'
        ));
      }

      // response.completed is published just before the final Chat status is
      // committed. Reconcile against Snapshot even when the local SSE state is
      // already terminal, so queued input cannot enter that commit window.
      if (refreshedChat && refreshedChat.status !== projection.chat.status) {
        replaceChat(refreshedChat);
      }
      return snapshotRunning;
    } catch {
      // A failed status check must not create concurrent Chats. The queue will
      // retry this check on its normal polling interval.
      return true;
    }
  }, [replaceChat]);

  const stopCurrentChat = useCallback(() => {
    const projection = stateRef.current.projections
      .find(({ chat }) => chat.id === stateRef.current.currentChatId);
    if (!projection || projection.chat.status !== 'running') return;
    runtimeStreamAbortRef.current?.abort();
    activeSubscriptionRef.current?.abort();
    userAbortedChatIdsRef.current.add(projection.chat.id);
    setConnectionStatus('aborted');
    void dataConsoleApi.stopChat(projection.chat.session_id, projection.chat.id)
      .then(({ chat }) => replaceChat(chat))
      .catch((stopError) => {
        setError(errorMessage(stopError));
        setConnectionStatus('error');
      });
  }, [replaceChat]);

  const answerClarification = useCallback(async (
    chatId: string,
    clarificationId: string,
    result: AskUserQuestionToolResult,
  ) => {
    const projection = stateRef.current.projections
      .find(({ chat }) => chat.id === chatId);
    const pending = projection && projection.chat.status === 'running'
      ? selectPendingAskUserQuestion(projection.messages)
      : null;
    if (!projection || pending?.id !== clarificationId) {
      throw new Error('当前 Chat 不在等待确认状态');
    }

    setError(null);
    try {
      const response = await dataConsoleApi.answerClarification(
        projection.chat.session_id,
        chatId,
        {
          clarification_id: clarificationId,
          result,
        },
      );
      if (response.chat.id !== chatId) {
        throw new Error('确认回答返回了不同的 Chat');
      }
    } catch (answerError) {
      setError(errorMessage(answerError));
      throw answerError;
    }
  }, []);

  const currentProjection = state.projections.find(
    ({ chat }) => chat.id === state.currentChatId,
  ) ?? null;
  const currentTimeline = useMemo(
    () => currentProjection ? selectChatTimeline(currentProjection) : [],
    [currentProjection],
  );
  const runtimeSession = useMemo(
    () => state.session
      ? buildRuntimeSession(state.session, state.projections)
      : draftRuntimeSession,
    [state.projections, state.session],
  );

  const startNewSession = useCallback(() => {
    activeSubscriptionRef.current?.abort();
    activeSubscriptionRef.current = null;
    runtimeStreamAbortRef.current?.abort();
    runtimeStreamAbortRef.current = null;
    activeRuntimeStreamsRef.current.clear();

    const next: ConsoleState = {
      session: null,
      projections: [],
      currentChatId: null,
    };
    stateRef.current = next;
    setState(next);
    setDraftQueueSessionId(createDraftQueueSessionId());
    setError(null);
    setModelConfigurationRequired(false);
    setConnectionStatus('idle');
  }, []);

  const reloadSessions = useCallback(() => {
    setSessionCatalogVersion((current) => current + 1);
  }, []);

  const renameSession = useCallback(async (
    sessionId: string,
    title: string,
  ): Promise<ConsoleSession> => {
    const { session } = await dataConsoleApi.updateSession(sessionId, { title });
    setSessions((current) => current.map((item) => (
      item.id === session.id ? session : item
    )));
    if (stateRef.current.session?.id === session.id) {
      const next = {
        ...stateRef.current,
        session,
      };
      stateRef.current = next;
      setState(next);
    }
    return session;
  }, []);

  const deleteSession = useCallback(async (sessionId: string): Promise<void> => {
    await dataConsoleApi.deleteSession(sessionId);
    setSessions((current) => current.filter((item) => item.id !== sessionId));
    if (stateRef.current.session?.id === sessionId) {
      startNewSession();
    }
  }, [startNewSession]);

  const submitFeedback = useCallback(async (
    chatId: string,
    kind: 'like' | 'dislike' | 'copy',
    detail?: { reason?: string | null; detail?: string | null },
  ) => {
    const session = stateRef.current.session;
    if (!session) throw new Error('会话尚未加载完成');
    await dataConsoleApi.createFeedback(session.id, chatId, { kind, ...detail });
  }, []);

  const getFeedbackReaction = useCallback(async (
    chatId: string,
  ): Promise<'like' | 'dislike' | null> => {
    const session = stateRef.current.session;
    if (!session) throw new Error('会话尚未加载完成');
    const result = await dataConsoleApi.getFeedbackReaction(session.id, chatId);
    return result.feedback?.kind ?? null;
  }, []);

  const clearFeedbackReaction = useCallback(async (chatId: string): Promise<void> => {
    const session = stateRef.current.session;
    if (!session) throw new Error('会话尚未加载完成');
    await dataConsoleApi.clearFeedbackReaction(session.id, chatId);
  }, []);

  const steerCurrentChat = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) throw new Error('请输入引导内容');
    const current = stateRef.current;
    const projection = current.projections.find(({ chat }) => chat.id === current.currentChatId);
    if (!current.session || !projection || projection.chat.status !== 'running') {
      throw new Error('当前没有可引导的运行中对话');
    }
    await dataConsoleApi.steer(current.session.id, projection.chat.id, trimmed);
  }, []);

  const editPlan = useCallback(async (
    chatId: string,
    plan: Plan,
    reason?: string | null,
  ) => {
    const current = stateRef.current;
    const session = current.session;
    const projection = current.projections.find(({ chat }) => chat.id === chatId);
    if (!session || !projection) throw new Error('当前 Chat 尚未加载完成');

    const response = await dataConsoleApi.editPlan(session.id, chatId, { plan, reason });
    const latest = stateRef.current;
    const projections = latest.projections.map((item) => (
      item.chat.id === chatId
        ? { ...item, chat: { ...item.chat, plan: response.plan } }
        : item
    ));
    const next = { ...latest, projections };
    stateRef.current = next;
    setState(next);
  }, []);

  const loadRawTrace = useCallback(async (chatId: string) => {
    const current = stateRef.current;
    const session = current.session;
    const projection = current.projections.find(({ chat }) => chat.id === chatId);
    if (!session || !projection) return;
    if (projection.messages.some((message) => !message.id.startsWith('error-'))) return;

    const trace = await dataConsoleApi.getTrace(session.id, chatId, 'raw');
    const latest = stateRef.current;
    if (latest.session?.id !== session.id) return;
    const projections = latest.projections.map((item) => {
      if (item.chat.id !== chatId) return item;
      const messages = withChatErrorMessage(
        trace.messages,
        chatId,
        item.lastSequenceNumber,
        item.chat.error,
      );
      return {
        ...item,
        messages,
        chat: { ...item.chat, plan: resolveChatPlan(item.chat, messages) },
      };
    });
    const next = { ...latest, projections };
    stateRef.current = next;
    setState(next);
  }, []);

  return {
    session: state.session,
    inputQueueSessionId: draftQueueSessionId,
    sessions,
    projections: state.projections,
    currentChatId: state.currentChatId,
    currentProjection,
    currentTimeline,
    datasources,
    selectedDatasourceId,
    runtimeSession,
    connectionStatus,
    error,
    modelConfigurationRequired,
    selectChat: (chatId) => {
      setState((current) => ({ ...current, currentChatId: chatId }));
    },
    selectDatasource: setSelectedDatasourceId,
    startNewSession,
    reloadSessions,
    renameSession,
    deleteSession,
    submitFeedback,
    getFeedbackReaction,
    clearFeedbackReaction,
    steerCurrentChat,
    editPlan,
    loadRawTrace,
    submitRuntimeRequest,
    isCurrentChatRunning,
    answerClarification,
    stopCurrentChat,
  };
}
