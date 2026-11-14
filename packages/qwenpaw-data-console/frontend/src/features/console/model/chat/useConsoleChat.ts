import { createDeferredBatch } from './deferredBatch';
import {
  createSessionArtifactsController,
  emptySessionArtifacts,
  type SessionArtifactsState,
} from '../artifacts/sessionArtifacts';
import { createReadCache, preserveUnchangedList } from './readCache';
import {
  historyCursor,
  hydrateProjectionTrace,
  createHistoryTraceQueue,
  mergeHistoryPage,
} from './sessionHistory';
import { modelConfigApi } from '../../api/modelConfig';
import {
  checkModelConfiguration,
  createModelConfigurationAdmission,
  type ModelConfigurationAdmission,
  ModelConfigurationRequiredError,
} from './modelConfigurationGate';
import type { IAgentScopeRuntimeWebUISession } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { ApiRequestError } from '@/services/request';
import { getHostAuthContext, getHostCurrentUser, subscribeHostAuth } from '@/services/auth';
import { getApiUrl } from '@/services/config';
import { createCMModelConfigGate } from '@/services/cmModelConfig';
import { useAppI18n } from '@/i18n';
import { dataConsoleApi } from '../../api/client';
import { subscribeChatEvents } from '../../api/events';
import { dispatchPendingUi, setUiSession } from '../../ui-runtime/runtime';
import type {
  AskUserQuestionToolResult,
  ArtifactComment,
  ConsoleChat,
  ConsoleSession,
  ConsoleStreamEvent,
  TraceMessage,
  TimingEvent,
  DatasourceOption,
  DatasetOption,
  Plan,
  SessionShareScope,
  ToolConfirmationDecision,
} from '../../api/types';
import {
  selectToolConfirmations,
  toolConfirmationAvailability,
  toolConfirmationCallId,
  isToolConfirmationResult,
} from './toolConfirmation';
import {
  confirmationIdentity,
  createToolConfirmationSubmissions,
  type ToolConfirmationSubmissions,
} from './toolConfirmationSubmission';
import { createMcpFailureMessage } from './mcpFailureMessage';
import { createDraftQueueSessionId } from './draftQueueSessionId';
import { clearAcceptedRuntimeInputDraft, moveRuntimeInputCache } from './runtimeInputCache';
import { selectPendingAskUserQuestion } from './askUserQuestion';
import { withRunningChatClarification } from './sessionClarification';
import {
  createStreamSettlementGuard,
  waitForStreamReconnect,
  isSnapshotSessionRunning,
  selectChatStreamCompletionAction,
} from '../runtime/chatStreamLifecycle';
import {
  createChatStreamProjection,
  projectStreamEvent,
  type ChatStreamProjection,
} from '../runtime/projectStream';
import { selectChatTimeline, type ChatTimelineItem } from '../runtime/selectChatTimeline';
import {
  createSteerArrival,
  SteerArrivalAbortedError,
  SteerChatEndedError,
  shouldForwardRuntimeStreamEvent,
} from '../runtime/runtimeSteer';
import {
  createRuntimeSessionBuilder,
  createRuntimeStreamBootstrapEvent,
  draftRuntimeSession,
} from '../runtime/runtimeMessages';
import { serializeRuntimeAttachmentIds } from '../runtime/runtimeRequest';
import { segmentInstanceKey } from '../runtime/segmentExpansion';
import { SessionAccess, hasSessionWriteAccess, isSessionReadOnly } from './sessionAccess';

export type ConsoleConnectionStatus =
  'loading' | 'idle' | 'connecting' | 'streaming' | 'eof' | 'aborted' | 'error';

export interface ConsoleChatController {
  sessionArtifacts: SessionArtifactsState;
  refreshSessionArtifacts: () => void;
  readOnly: boolean; // No write access until ownership is positively established.
  accessVersion: string; // Invalidates mounted input and stale callbacks on identity/navigation changes.
  captureWriteAccess: (sessionId?: string | null) => () => void; // Recheck before writes and after awaits.
  updateShareScope: (sessionId: string, scope: SessionShareScope) => Promise<ConsoleSession>;
  session: ConsoleSession | null;
  initialHistoricalSegmentKeys: ReadonlySet<string>;
  inputQueueSessionId: string;
  inputSessionEpoch: number; // Changes on navigation, but not when the first send creates a session.
  sessions: ConsoleSession[];
  sessionsLoadingMore: boolean;
  sessionsHasMore: boolean;
  searchSessions: (searchText: string) => void;
  loadMoreSessions: () => void;
  projections: ChatStreamProjection[];
  currentChatId: string | null;
  currentProjection: ChatStreamProjection | null;
  currentTimeline: ChatTimelineItem[];
  datasources: DatasourceOption[];
  selectedDatasourceId: string | null;
  datasets: DatasetOption[];
  datasetsLoading: boolean;
  datasetsError: string | null;
  selectedDatasetNames: string[];
  selectDatasets: (names: string[]) => void;
  reloadDatasets: () => void;
  runtimeSession: IAgentScopeRuntimeWebUISession | null;
  runtimeSnapshotVersion: number;
  historyVersion: number;
  hasMoreHistory: boolean;
  historyLoading: boolean;
  historyError: string | null;
  loadOlderChats: () => Promise<void>;
  ensureChatTrace: (chatId: string) => void;
  deferHistoryUpdates: () => void;
  connectionStatus: ConsoleConnectionStatus;
  error: string | null;
  modelConfigurationRequired: boolean;
  ensureModelConfiguration: (signal?: AbortSignal) => Promise<ModelConfigurationAdmission>;
  cmModelConfigurationRequired: boolean;
  checkCMModelConfiguration: () => Promise<boolean>;
  selectChat: (chatId: string) => void;
  selectDatasource: (datasourceId: string | null) => void;
  startNewSession: () => void;
  reloadSessions: () => void;
  renameSession: (sessionId: string, title: string) => Promise<ConsoleSession>;
  enableClarification: boolean;
  setEnableClarification: (enableClarification: boolean) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  forkChat: (chatId: string) => Promise<void>;
  submitFeedback: (
    chatId: string,
    kind: 'like' | 'dislike' | 'copy',
    detail?: { reason?: string | null; detail?: string | null },
  ) => Promise<void>;
  getFeedbackReaction: (chatId: string) => Promise<'like' | 'dislike' | null>;
  clearFeedbackReaction: (chatId: string) => Promise<void>;
  steerCurrentChat: (text: string, artifactComments?: ArtifactComment[]) => Promise<void>;
  ensureSession: (title?: string) => Promise<ConsoleSession>;
  uploadAttachment: (file: File) => Promise<{ attachment_id: string; filename: string }>;
  discardAttachment: (attachmentId: string) => Promise<void>;
  submitRuntimeRequest: (
    text: string,
    signal?: AbortSignal,
    attachmentIds?: string[],
    artifactComments?: ArtifactComment[],
    admission?: ModelConfigurationAdmission,
  ) => Promise<Response>;
  reconnectRuntimeRequest: (sessionId: string, signal?: AbortSignal) => Promise<Response>;
  isCurrentChatRunning: () => Promise<boolean>;
  answerClarification: (
    chatId: string,
    clarificationId: string,
    result: AskUserQuestionToolResult,
  ) => Promise<void>;
  editPlan: (chatId: string, plan: Plan, reason?: string | null) => Promise<void>;
  stopCurrentChat: () => void;
  toolConfirmationSubmissions: ToolConfirmationSubmissions;
  toolConfirmationReady: boolean;
  toolConfirmationStopping: boolean;
  answerToolConfirmation: (
    chatId: string,
    callId: string,
    decision: ToolConfirmationDecision,
  ) => Promise<void>;
}

interface ConsoleState {
  session: ConsoleSession | null;
  projections: ChatStreamProjection[];
  currentChatId: string | null;
  initialHistoricalSegmentKeys: ReadonlySet<string>;
}

const EMPTY_HISTORICAL_SEGMENT_KEYS: ReadonlySet<string> = new Set();
const SIDEBAR_SESSION_PAGE_SIZE = 20;

function matchesSidebarQuery(session: ConsoleSession, searchText: string) {
  return (
    session.chat_count > 0 &&
    (!searchText || session.title.toLocaleLowerCase().includes(searchText.toLocaleLowerCase()))
  );
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

function pickDatasourceId(
  items: DatasourceOption[],
  requestedDatasourceId?: string | null,
): string | null {
  const requested = requestedDatasourceId?.trim() || '';
  if (requested && items.some((item) => item.id === requested)) {
    return requested;
  }
  return null;
}

export function useConsoleChat(
  requestedSessionId?: string | null,
  onSessionResolved?: (session: ConsoleSession) => void,
  requestedDatasourceId?: string | null,
): ConsoleChatController {
  const { t } = useAppI18n();
  const auth = useSyncExternalStore(subscribeHostAuth, getHostAuthContext);
  const [state, setState] = useState<ConsoleState>({
    session: null,
    projections: [],
    currentChatId: null,
    initialHistoricalSegmentKeys: EMPTY_HISTORICAL_SEGMENT_KEYS,
  });
  const [draftQueueSessionId, setDraftQueueSessionId] = useState(createDraftQueueSessionId);
  const [draftEnableClarification, setDraftEnableClarification] = useState(true);
  const draftEnableClarificationRef = useRef(draftEnableClarification);
  draftEnableClarificationRef.current = draftEnableClarification;
  const [datasources, setDatasources] = useState<DatasourceOption[]>([]);
  const [sessions, setSessions] = useState<ConsoleSession[]>([]);
  const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false);
  const [sessionsTotal, setSessionsTotal] = useState(0);
  const [sessionSearchText, setSessionSearchText] = useState('');
  const sessionSearchTextRef = useRef('');
  const [selectedDatasourceId, setSelectedDatasourceId] = useState<string | null>(null);
  const [datasets, setDatasets] = useState<DatasetOption[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const [datasetsVersion, setDatasetsVersion] = useState(0);
  const [selectedDatasetNames, setSelectedDatasetNames] = useState<string[]>([]);
  const selectedDatasetNamesRef = useRef<string[]>([]);
  const selectDatasets = useCallback((names: string[]) => {
    selectedDatasetNamesRef.current = [...names];
    setSelectedDatasetNames([...names]);
  }, []);
  const reloadSessionCatalogRef = useRef<() => void>(() => {});
  const sessionCatalogAbortRef = useRef<AbortController | null>(null);
  const sessionCatalogRef = useRef({ page: 0, total: 0, searchText: '', loading: false });
  const [connectionStatus, setConnectionStatus] = useState<ConsoleConnectionStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const [modelConfigurationRequired, setModelConfigurationRequired] = useState(false);
  const cmModelGate = useMemo(
    () => ({
      revision: auth.revision,
      ...createCMModelConfigGate(),
    }),
    [auth.revision],
  );
  const cmModelConfigurationRequired = useSyncExternalStore(
    cmModelGate.subscribe,
    cmModelGate.getSnapshot,
  );
  const [runtimeSnapshotVersion, setRuntimeSnapshotVersion] = useState(0);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [historyCursorValue, setHistoryCursorValue] = useState<number | null>(null);
  const historyCursorRef = useRef<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const historyRequestRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  const confirmationTracePendingRef = useRef(new Set<string>());
  const [, refreshConfirmation] = useState(0);
  const confirmationIdentityKey = confirmationIdentity(
    new URL(getApiUrl('/'), window.location.href).href,
    getHostCurrentUser(),
  );
  const toolConfirmationSubmissions = useMemo(
    () => createToolConfirmationSubmissions(confirmationIdentityKey),
    [confirmationIdentityKey],
  );
  useEffect(() => {
    if (!state.session) return;
    toolConfirmationSubmissions.reconcile(
      state.session.id,
      state.projections.map(({ chat, messages }) => ({
        chatId: chat.id,
        terminal: chat.status !== 'running' && chat.status !== 'created',
        resolvedCallIds: new Set(
          messages
            .filter((item) => item.chat_id === chat.id && isToolConfirmationResult(item))
            .map(toolConfirmationCallId)
            .filter((id): id is string => id !== null),
        ),
      })),
    );
  }, [state.session, state.projections, toolConfirmationSubmissions]);
  const sessionEpochRef = useRef(0);
  const [sessionArtifacts, setSessionArtifacts] = useState(emptySessionArtifacts);
  const [artifactController] = useState(() =>
    createSessionArtifactsController(dataConsoleApi.getSessionArtifacts, setSessionArtifacts),
  );
  const ensureArtifactScope = useCallback(
    (sessionId: string | null) => {
      const context = getHostAuthContext();
      artifactController.ensureScope(
        sessionId && context.ready
          ? {
              sessionId,
              revision: context.revision,
              epoch: sessionEpochRef.current,
            }
          : null,
      );
    },
    [artifactController],
  );
  const refreshSessionArtifacts = useCallback(() => {
    void artifactController.refresh();
  }, [artifactController]);
  const sessionLoadAbortRef = useRef<AbortController | null>(null);
  const selectedDatasourceIdRef = useRef(selectedDatasourceId);
  const userAbortedChatIdsRef = useRef(new Set<string>());
  const runtimeStreamAbortRef = useRef<AbortController | null>(null);
  const runtimeSessionCreationRef = useRef<Promise<ConsoleSession> | null>(null);
  const uploadedAttachmentSessionsRef = useRef(new Map<string, string>());
  const onSessionResolvedRef = useRef(onSessionResolved);
  const hasLoadedSessionCatalogRef = useRef(false);
  const reportedSessionRevisitRef = useRef<string | null>(null);
  const [steerArrival] = useState(() => createSteerArrival());
  onSessionResolvedRef.current = onSessionResolved;

  const accessRef = useRef(new SessionAccess(auth.revision));
  if (accessRef.current.revision !== auth.revision)
    accessRef.current = new SessionAccess(auth.revision);
  useEffect(() => {
    const session = state.session;
    if (!session || !accessRef.current.canWrite(session.id)) return;
    for (const projection of state.projections) {
      if (projection.chat.session_id === session.id) dispatchPendingUi(projection);
    }
  }, [state.session, state.projections]);
  const [, refreshAccess] = useState(0);
  const navigationRef = useRef({
    id: requestedSessionId ?? null,
    revision: auth.revision,
    version: 0,
  });
  const navigationChanged =
    navigationRef.current.id !== (requestedSessionId ?? null) ||
    navigationRef.current.revision !== auth.revision;
  // A draft becoming its persisted Session must keep the admitted first send.
  const draftResolved =
    !navigationRef.current.id &&
    requestedSessionId === stateRef.current.session?.id &&
    navigationRef.current.revision === auth.revision;
  const navigationVersion =
    navigationRef.current.version + Number(navigationChanged && !draftResolved);
  // Concurrent route renders can be discarded or interrupted by the old URL.
  // Publish only committed navigation; mutating this ref during render would
  // make that old URL look like another session and remount the active SDK.
  useLayoutEffect(() => {
    if (navigationChanged && !draftResolved) artifactController.ensureScope(null);
    navigationRef.current = {
      id: requestedSessionId ?? null,
      revision: auth.revision,
      version: navigationVersion,
    };
  }, [
    requestedSessionId,
    auth.revision,
    navigationVersion,
    navigationChanged,
    draftResolved,
    artifactController,
  ]);
  const captureWriteAccess = useCallback(
    (sessionId?: string | null) => {
      if (
        sessionId === undefined &&
        requestedSessionId &&
        stateRef.current.session?.id !== requestedSessionId
      ) {
        throw new Error(t('sessionShare.readOnly'));
      }
      const context = {
        authRevision: auth.revision,
        navigationVersion,
        sessionEpoch: sessionEpochRef.current,
        sessionId:
          sessionId === undefined
            ? (stateRef.current.session?.id ?? requestedSessionId ?? null)
            : sessionId,
      };
      const assert = () => {
        const currentAuth = getHostAuthContext();
        if (
          !hasSessionWriteAccess(accessRef.current, context, {
            authRevision: currentAuth.revision,
            ready: currentAuth.ready,
            navigationVersion: navigationRef.current.version,
            sessionEpoch: sessionEpochRef.current,
          })
        )
          throw new Error(t('sessionShare.readOnly'));
      };
      assert();
      return assert;
    },
    [auth.revision, navigationVersion, requestedSessionId, t],
  );
  // URL navigation may render before setState commits the newly created session.
  // Match captureWriteAccess/navigationRef so the first stream keeps its mounted SDK.
  const readOnly = isSessionReadOnly(
    accessRef.current,
    auth,
    requestedSessionId,
    stateRef.current.session?.id ?? null,
  );

  useEffect(() => {
    return () => {
      steerArrival.failAll(new SteerArrivalAbortedError());
    };
  }, [navigationVersion, steerArrival]);

  useEffect(() => {
    const controller = new AbortController();
    const selectedDatasource = datasources.find((item) => item.id === selectedDatasourceId);
    const supportsDatasets = Boolean(selectedDatasource);
    setDatasets([]);
    setDatasetsError(null);
    setDatasetsLoading(Boolean(selectedDatasourceId) && !readOnly && supportsDatasets);
    if (selectedDatasourceId && !readOnly && supportsDatasets) {
      void dataConsoleApi
        .listDatasets(selectedDatasourceId, controller.signal)
        .then(({ items }) => {
          if (!controller.signal.aborted) setDatasets(items);
        })
        .catch((loadError) => {
          if (!controller.signal.aborted) setDatasetsError(errorMessage(loadError));
        })
        .finally(() => {
          if (!controller.signal.aborted) setDatasetsLoading(false);
        });
    }
    return () => controller.abort();
  }, [selectedDatasourceId, datasetsVersion, readOnly, auth.revision, datasources]);

  const selectDatasource = useCallback(
    (id: string | null) => {
      if (stateRef.current.session || runtimeSessionCreationRef.current) return;
      selectedDatasourceIdRef.current = id;
      setSelectedDatasourceId(id);
      selectDatasets([]);
    },
    [selectDatasets],
  );

  // 配置提示独立于会话错误；切换会话不清除，返回当前 tab 时重新检查。
  useEffect(() => {
    if (!auth.ready) return undefined;
    void cmModelGate.check();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void cmModelGate.check();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [auth.ready, cmModelGate]);

  useEffect(() => {
    let refreshPending = false;
    let disposed = false;
    const refresh = () => {
      if (disposed || !auth.ready) return;
      if (sessionCatalogRef.current.loading) {
        refreshPending = true;
        return;
      }
      const controller = new AbortController();
      sessionCatalogAbortRef.current = controller;
      sessionCatalogRef.current = {
        page: sessionCatalogRef.current.page,
        total: sessionCatalogRef.current.total,
        searchText: sessionSearchText,
        loading: true,
      };
      const access = accessRef.current;
      const previousStatus = access.status;
      const request = access.beginCatalog();
      if (access.status !== previousStatus) refreshAccess((version) => version + 1);
      void dataConsoleApi
        .listSessions(
          {
            search_text: sessionSearchText || undefined,
            sort: 'updated_desc',
            page: 1,
            page_size: SIDEBAR_SESSION_PAGE_SIZE,
            min_chat_count: 1,
          },
          controller.signal,
        )
        .then(({ items, total }) => {
          if (controller.signal.aborted || getHostAuthContext().revision !== access.revision)
            return;
          const target = navigationRef.current.id ?? stateRef.current.session?.id ?? null;
          const couldWrite = access.canWrite(target);
          const status = access.status;
          const ownerIds = items.map((item) => item.id);
          // Searching or refreshing the sidebar must not revoke the active session.
          if (target && couldWrite && !ownerIds.includes(target)) ownerIds.push(target);
          if (!access.completeCatalog(request, ownerIds)) return;
          if (status !== access.status || couldWrite !== access.canWrite(target)) {
            refreshAccess((version) => version + 1);
          }
          hasLoadedSessionCatalogRef.current = true;
          sessionCatalogRef.current = {
            page: 1,
            total,
            searchText: sessionSearchText,
            loading: false,
          };
          setSessionsTotal(total);
          setSessions((current) => preserveUnchangedList(current, items));
        })
        .catch((catalogError) => {
          if (controller.signal.aborted || getHostAuthContext().revision !== access.revision)
            return;
          if (access.failCatalog(request)) refreshAccess((version) => version + 1);
          if (hasLoadedSessionCatalogRef.current) return;
          setError(t('console.sessionListLoadFailed', { message: errorMessage(catalogError) }));
        })
        .finally(() => {
          if (sessionCatalogAbortRef.current === controller) {
            sessionCatalogAbortRef.current = null;
            sessionCatalogRef.current = { ...sessionCatalogRef.current, loading: false };
          }
          if (refreshPending) {
            refreshPending = false;
            refresh();
          }
        });
    };
    reloadSessionCatalogRef.current = refresh;
    sessionCatalogAbortRef.current?.abort();
    sessionCatalogRef.current = {
      page: 0,
      total: 0,
      searchText: sessionSearchText,
      loading: false,
    };
    setSessionsLoadingMore(false);
    if (!auth.ready) {
      setSessions([]);
      setSessionsTotal(0);
    }
    refresh();
    return () => {
      disposed = true;
      sessionCatalogAbortRef.current?.abort();
      reloadSessionCatalogRef.current = () => {};
    };
  }, [t, auth.revision, auth.ready, sessionSearchText]);

  const searchSessions = useCallback((searchText: string) => {
    const normalized = searchText.trim();
    sessionSearchTextRef.current = normalized;
    setSessionSearchText(normalized);
  }, []);

  const loadMoreSessions = useCallback(() => {
    const catalog = sessionCatalogRef.current;
    if (!auth.ready || catalog.loading || catalog.page * SIDEBAR_SESSION_PAGE_SIZE >= catalog.total)
      return;
    const controller = new AbortController();
    sessionCatalogAbortRef.current = controller;
    sessionCatalogRef.current = { ...catalog, loading: true };
    setSessionsLoadingMore(true);
    const revision = auth.revision;
    const page = catalog.page + 1;
    void dataConsoleApi
      .listSessions(
        {
          search_text: catalog.searchText || undefined,
          sort: 'updated_desc',
          page,
          page_size: SIDEBAR_SESSION_PAGE_SIZE,
          min_chat_count: 1,
        },
        controller.signal,
      )
      .then(({ items, total }) => {
        if (
          controller.signal.aborted ||
          getHostAuthContext().revision !== revision ||
          sessionCatalogRef.current.searchText !== catalog.searchText
        )
          return;
        accessRef.current.extendCatalog(items.map((item) => item.id));
        setSessions((current) => {
          const incomingIds = new Set(items.map((item) => item.id));
          return preserveUnchangedList(current, [
            ...current.filter((item) => !incomingIds.has(item.id)),
            ...items,
          ]);
        });
        setSessionsTotal(total);
        sessionCatalogRef.current = {
          page,
          total,
          searchText: catalog.searchText,
          loading: false,
        };
      })
      .catch((catalogError) => {
        if (!controller.signal.aborted && getHostAuthContext().revision === revision) {
          setError(t('console.sessionListLoadFailed', { message: errorMessage(catalogError) }));
        }
      })
      .finally(() => {
        if (sessionCatalogAbortRef.current === controller) {
          sessionCatalogAbortRef.current = null;
          sessionCatalogRef.current = { ...sessionCatalogRef.current, loading: false };
          setSessionsLoadingMore(false);
        }
      });
  }, [auth.ready, auth.revision, t]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    selectedDatasourceIdRef.current = selectedDatasourceId;
  }, [selectedDatasourceId]);

  const replaceChat = useCallback(
    (chat: ConsoleChat) => {
      const current = stateRef.current;
      if (current.session?.id !== chat.session_id) return;
      const existing = current.projections.find(({ chat: item }) => item.id === chat.id);
      const nextProjection = existing
        ? {
            ...existing,
            chat,
            lastSequenceNumber: chat.last_sequence_number,
          }
        : createChatStreamProjection(chat);
      const projections = existing
        ? current.projections.map((projection) =>
            projection.chat.id === chat.id ? nextProjection : projection,
          )
        : [...current.projections, nextProjection];
      const hasRunningChat = projections.some(({ chat: item }) => item.status === 'running');
      const next = {
        session: current.session
          ? {
              ...current.session,
              status: hasRunningChat ? ('running' as const) : ('idle' as const),
              chat_count: Math.max(current.session.chat_count, projections.length),
            }
          : null,
        projections,
        currentChatId: chat.id,
        initialHistoricalSegmentKeys: current.initialHistoricalSegmentKeys,
      };
      stateRef.current = next;
      setState(next);
      steerArrival.observe(nextProjection);
      if (next.session && accessRef.current.canWrite(next.session.id)) {
        setSessions((current) =>
          matchesSidebarQuery(next.session as ConsoleSession, sessionSearchTextRef.current)
            ? [
                next.session as ConsoleSession,
                ...current.filter((session) => session.id !== next.session?.id),
              ]
            : current.filter((session) => session.id !== next.session?.id),
        );
      }
    },
    [steerArrival],
  );

  const applyEvent = useCallback(
    (event: ConsoleStreamEvent) => {
      const current = stateRef.current;
      if (event.object === 'artifact.registered' && current.session) {
        ensureArtifactScope(current.session.id);
        artifactController.register(event.artifact);
      }
      if (event.object === 'segment' && current.session) {
        ensureArtifactScope(current.session.id);
        artifactController.segmentChanged();
      }
      const projectionIndex = current.projections.findIndex(
        ({ chat }) => chat.id === event.chat_id,
      );
      if (projectionIndex < 0) return;
      const currentProjection = current.projections[projectionIndex];
      const nextProjection = projectStreamEvent(currentProjection, event);
      if (nextProjection === currentProjection) return;
      const projections = [...current.projections];
      projections[projectionIndex] = nextProjection;
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
      steerArrival.observe(nextProjection);
    },
    [steerArrival, ensureArtifactScope, artifactController],
  );

  const historyInteractionUntil = useRef(0);
  const deferHistoryUpdates = useCallback(() => {
    historyInteractionUntil.current = Date.now() + 180;
  }, []);
  const traceQueueRef = useRef<{ signal: AbortSignal; enqueue: (chats: ConsoleChat[]) => void }>();
  const hydrateTraces = useCallback(
    (chats: ConsoleChat[], signal: AbortSignal) => {
      if (signal.aborted) return;
      if (traceQueueRef.current?.signal === signal) {
        traceQueueRef.current.enqueue(chats);
        return;
      }
      const epoch = sessionEpochRef.current;
      const revision = getHostAuthContext().revision;
      let failedCount = 0;
      const batch = createDeferredBatch<{
        chat: ConsoleChat;
        messages: TraceMessage[];
        timingEvents: TimingEvent[];
      }>(
        (items) => {
          if (
            signal.aborted ||
            epoch !== sessionEpochRef.current ||
            revision !== getHostAuthContext().revision
          )
            return;
          const current = stateRef.current;
          const updates = new Map(
            items
              .filter((item) => item.chat.session_id === current.session?.id)
              .map((item) => [item.chat.id, item]),
          );
          const projections = current.projections.map((projection) => {
            const update = updates.get(projection.chat.id);
            if (update) confirmationTracePendingRef.current.delete(projection.chat.id);
            return update
              ? hydrateProjectionTrace(projection, update.messages, update.timingEvents)
              : projection;
          });
          refreshConfirmation((version) => version + 1);
          if (projections.every((projection, index) => projection === current.projections[index]))
            return;
          const next = { ...current, projections };
          stateRef.current = next;
          setState(next);
          setHistoryVersion((version) => version + 1);
        },
        (flush) => {
          const deadline = Date.now() + 750;
          const run = () => {
            const remaining = Math.min(historyInteractionUntil.current, deadline) - Date.now();
            if (remaining > 0) {
              timer = setTimeout(run, remaining);
              return;
            }
            flush();
          };
          let timer = setTimeout(run, 80);
          return () => clearTimeout(timer);
        },
      );
      signal.addEventListener('abort', batch.dispose, { once: true });
      const enqueue = createHistoryTraceQueue(
        signal,
        async (chat) => {
          const trace = await dataConsoleApi.getTrace(chat.session_id, chat.id, 'raw', signal);
          return {
            timingEvents: trace.timing_events ?? [],
            messages: [
              ...trace.messages,
              ...(trace.mcp_connections ?? []).flatMap((event) => {
                if (event.session_id !== chat.session_id || event.chat_id !== chat.id) return [];
                const message = createMcpFailureMessage(event);
                return message ? [message] : [];
              }),
            ],
          };
        },
        (chat, trace) => batch.push({ chat, ...trace }),
        () => {
          if (epoch === sessionEpochRef.current && revision === getHostAuthContext().revision) {
            setError(t('console.traceLoadFailed', { count: ++failedCount }));
          }
        },
      );
      traceQueueRef.current = { signal, enqueue };
      enqueue(chats);
    },
    [t],
  );

  const ensureChatTrace = useCallback(
    (chatId: string) => {
      const scope = sessionLoadAbortRef.current;
      const projection = stateRef.current.projections.find((item) => item.chat.id === chatId);
      if (scope && projection) hydrateTraces([projection.chat], scope.signal);
    },
    [hydrateTraces],
  );

  const selectChat = useCallback(
    (chatId: string) => {
      ensureChatTrace(chatId);
      setState((current) =>
        current.currentChatId === chatId ? current : { ...current, currentChatId: chatId },
      );
    },
    [ensureChatTrace],
  );

  const loadOlderChats = useCallback(async () => {
    const current = stateRef.current;
    const before = historyCursorRef.current;
    const scope = sessionLoadAbortRef.current;
    if (
      !current.session ||
      before === null ||
      historyRequestRef.current ||
      !scope ||
      scope.signal.aborted
    )
      return;
    const request = new AbortController();
    historyRequestRef.current = request;
    const abort = () => request.abort();
    scope.signal.addEventListener('abort', abort, { once: true });
    const sessionId = current.session.id;
    const revision = getHostAuthContext().revision;
    const isCurrent = () =>
      !request.signal.aborted &&
      revision === getHostAuthContext().revision &&
      stateRef.current.session?.id === sessionId &&
      sessionLoadAbortRef.current === scope;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const page = await dataConsoleApi.getSnapshot(sessionId, request.signal, before);
      if (!isCurrent()) return;
      const latest = stateRef.current;
      const added = page.chats.filter(
        (chat) =>
          chat.sequence < before && !latest.projections.some((item) => item.chat.id === chat.id),
      );
      const nextCursor = historyCursor(page);
      // Fail closed if a server returns an overlapping/non-advancing page.
      historyCursorRef.current = nextCursor !== null && nextCursor < before ? nextCursor : null;
      setHistoryCursorValue(historyCursorRef.current);
      const next = {
        ...latest,
        projections: mergeHistoryPage(latest.projections, added),
        initialHistoricalSegmentKeys: new Set([
          ...latest.initialHistoricalSegmentKeys,
          ...added.flatMap((chat) =>
            chat.segments.map((segment) => segmentInstanceKey(chat.id, segment.segment_id)),
          ),
        ]),
      };
      stateRef.current = next;
      setState(next);
      setHistoryVersion((version) => version + 1);
      // Detail traces are requested by visible responses or an explicit turn selection.
    } catch (error) {
      if (isCurrent()) setHistoryError(errorMessage(error));
    } finally {
      scope.signal.removeEventListener('abort', abort);
      if (historyRequestRef.current === request) {
        historyRequestRef.current = null;
        setHistoryLoading(false);
      }
    }
  }, []);

  useEffect(
    () => () => {
      historyRequestRef.current?.abort();
      historyRequestRef.current = null;
      historyCursorRef.current = null;
      setHistoryCursorValue(null);
      setHistoryLoading(false);
      setHistoryError(null);
      sessionEpochRef.current += 1;
      artifactController.dispose();
      sessionLoadAbortRef.current?.abort();
      runtimeStreamAbortRef.current?.abort();
    },
    [artifactController],
  );

  const createRuntimeSession = useCallback(
    async (title: string): Promise<ConsoleSession> => {
      const assertAccess = captureWriteAccess();
      const existing = stateRef.current.session;
      if (existing) return existing;
      if (requestedSessionId) throw new Error(t('runtime.sessionNotLoaded'));
      if (runtimeSessionCreationRef.current) {
        return runtimeSessionCreationRef.current;
      }

      const epoch = sessionEpochRef.current;
      const creation = (async () => {
        const datasourceId = selectedDatasourceIdRef.current;

        const created = await dataConsoleApi.createSession({
          title: title.trim().slice(0, 48) || t('console.newSession'),
          datasource_id: datasourceId,
          enable_clarification: draftEnableClarificationRef.current,
        });
        assertAccess();
        if (epoch !== sessionEpochRef.current) {
          throw new DOMException('Session changed', 'AbortError');
        }
        const session = created.session;
        accessRef.current.recordCreated(session.id);
        refreshAccess((version) => version + 1);
        moveRuntimeInputCache(draftQueueSessionId, session.id);
        const next: ConsoleState = {
          session,
          projections: [],
          currentChatId: null,
          initialHistoricalSegmentKeys: EMPTY_HISTORICAL_SEGMENT_KEYS,
        };
        stateRef.current = next;
        setState(next);
        ensureArtifactScope(session.id);
        onSessionResolvedRef.current?.(session);
        return session;
      })();
      runtimeSessionCreationRef.current = creation;
      try {
        return await creation;
      } finally {
        if (runtimeSessionCreationRef.current === creation) {
          runtimeSessionCreationRef.current = null;
        }
      }
    },
    [draftQueueSessionId, requestedSessionId, t, captureWriteAccess, ensureArtifactScope],
  );

  const datasourceCache = useMemo(
    () => ({ revision: auth.revision, ...createReadCache<DatasourceOption[]>(30_000) }),
    [auth.revision],
  );
  const reactionCache = useMemo(
    () => ({
      revision: auth.revision,
      sessionId: requestedSessionId,
      ...createReadCache<'like' | 'dislike' | null>(30_000),
    }),
    [auth.revision, requestedSessionId],
  );
  const sessionStatusCache = useMemo(
    () => ({ revision: auth.revision, ...createReadCache<ConsoleSession>(0) }),
    [auth.revision],
  );

  const loadedAuthRevisionRef = useRef(auth.revision);
  useEffect(() => {
    const identityChanged = loadedAuthRevisionRef.current !== auth.revision;
    loadedAuthRevisionRef.current = auth.revision;
    if (
      !identityChanged &&
      auth.ready &&
      requestedSessionId &&
      requestedSessionId === stateRef.current.session?.id
    ) {
      return;
    }

    historyRequestRef.current?.abort();
    historyRequestRef.current = null;
    historyCursorRef.current = null;
    setHistoryCursorValue(null);
    setHistoryLoading(false);
    setHistoryError(null);
    sessionEpochRef.current += 1;
    ensureArtifactScope(requestedSessionId ?? null);
    runtimeSessionCreationRef.current = null;
    runtimeStreamAbortRef.current?.abort();
    runtimeStreamAbortRef.current = null;
    setDraftEnableClarification(true);
    const controller = new AbortController();
    sessionLoadAbortRef.current = controller;
    const empty: ConsoleState = {
      session: null,
      projections: [],
      currentChatId: null,
      initialHistoricalSegmentKeys: EMPTY_HISTORICAL_SEGMENT_KEYS,
    };
    stateRef.current = empty;
    setState(empty);
    selectedDatasourceIdRef.current = null;
    setSelectedDatasourceId(null);
    setModelConfigurationRequired(false);
    if (!auth.ready) return () => controller.abort();

    const load = async () => {
      setConnectionStatus('loading');
      setError(null);
      selectDatasets([]);

      try {
        void (async () => {
          let items: DatasourceOption[] = [];
          try {
            items = await datasourceCache.get(
              'datasources',
              async () => (await dataConsoleApi.listDatasources()).items,
            );
          } catch (firstError) {
            try {
              items = await datasourceCache.get(
                'datasources',
                async () => (await dataConsoleApi.listDatasources()).items,
              );
            } catch {
              if (controller.signal.aborted) return;
              setError(
                t('console.datasourceLoadFailed', {
                  message: errorMessage(firstError),
                }),
              );
            }
          }
          if (controller.signal.aborted) return;
          setDatasources(items);

          if (!requestedSessionId) {
            const datasourceId = pickDatasourceId(items, requestedDatasourceId);
            selectedDatasourceIdRef.current = datasourceId;
            setSelectedDatasourceId(datasourceId);
          }
        })();

        if (!requestedSessionId) {
          const next: ConsoleState = {
            session: null,
            projections: [],
            currentChatId: null,
            initialHistoricalSegmentKeys: EMPTY_HISTORICAL_SEGMENT_KEYS,
          };
          stateRef.current = next;
          setState(next);
          setConnectionStatus('idle');
          return;
        }

        if (!accessRef.current.canWrite(requestedSessionId)) {
          void dataConsoleApi
            .listSessions(
              {
                session_id: requestedSessionId,
                page: 1,
                page_size: 1,
              },
              controller.signal,
            )
            .then(({ items }) => {
              if (controller.signal.aborted || getHostAuthContext().revision !== auth.revision)
                return;
              if (!items.some((item) => item.id === requestedSessionId)) return;
              accessRef.current.confirmOwned(requestedSessionId);
              refreshAccess((version) => version + 1);
            })
            .catch(() => {
              // A shared readable session intentionally remains read only.
            });
        }

        const snapshot = await dataConsoleApi.getSnapshot(requestedSessionId, controller.signal);
        if (controller.signal.aborted) return;

        historyCursorRef.current = historyCursor(snapshot);
        setHistoryCursorValue(historyCursorRef.current);
        const projections = snapshot.chats.map((chat) => createChatStreamProjection(chat));
        confirmationTracePendingRef.current = new Set(snapshot.chats.map((chat) => chat.id));
        const latestChat = [...snapshot.chats].sort(
          (left, right) => right.sequence - left.sequence,
        )[0];
        if (
          snapshot.session.chat_count > 0 &&
          reportedSessionRevisitRef.current !== requestedSessionId
        ) {
          reportedSessionRevisitRef.current = requestedSessionId;
          void dataConsoleApi
            .reportBehaviorEvent({
              kind: 'session_revisit',
              session_id: requestedSessionId,
              chat_id: latestChat?.id ?? null,
            })
            .catch(() => undefined);
        }
        const next = {
          session: snapshot.session,
          projections,
          currentChatId: latestChat?.id ?? null,
          // Freeze only the Segment instances present when this historical Snapshot opens.
          // Segments appended by later SSE events stay outside this set and open normally.
          initialHistoricalSegmentKeys: new Set(
            projections.flatMap((projection) =>
              projection.chat.segments.map((segment) =>
                segmentInstanceKey(projection.chat.id, segment.segment_id),
              ),
            ),
          ),
        };
        stateRef.current = next;
        setState(next);
        setRuntimeSnapshotVersion((version) => version + 1);
        const sessionDatasourceId = snapshot.session.datasource_id ?? null;
        selectedDatasourceIdRef.current = sessionDatasourceId;
        setSelectedDatasourceId(sessionDatasourceId);
        selectDatasets(latestChat?.dataset_names ?? []);
        setConnectionStatus('idle');
        void hydrateTraces(
          snapshot.chats.filter((chat) => chat.id === latestChat?.id || chat.status === 'running'),
          controller.signal,
        );
      } catch (loadError) {
        if (controller.signal.aborted) return;
        setError(
          loadError instanceof ApiRequestError && loadError.status === 404
            ? t('sessionShare.unavailable')
            : errorMessage(loadError),
        );
        setConnectionStatus('error');
      }
    };

    void load();
    return () => controller.abort();
  }, [
    requestedSessionId,
    requestedDatasourceId,
    selectDatasets,
    t,
    auth.revision,
    auth.ready,
    hydrateTraces,
    datasourceCache,
    ensureArtifactScope,
  ]);

  const ensureSession = useCallback(
    (title?: string) => createRuntimeSession(title ?? ''),
    [createRuntimeSession],
  );

  const uploadAttachment = useCallback(
    async (file: File) => {
      const assertAccess = captureWriteAccess();
      const session = await createRuntimeSession(file.name);
      assertAccess();
      captureWriteAccess(session.id);
      const { attachment } = await dataConsoleApi.uploadAttachment(session.id, file);
      assertAccess();
      uploadedAttachmentSessionsRef.current.set(attachment.attachment_id, session.id);
      return attachment;
    },
    [createRuntimeSession, captureWriteAccess],
  );

  const discardAttachment = useCallback(
    async (attachmentId: string) => {
      captureWriteAccess();
      if (
        uploadedAttachmentSessionsRef.current.get(attachmentId) !== stateRef.current.session?.id
      ) {
        throw new Error(t('sessionShare.readOnly'));
      }
      await dataConsoleApi.deleteAttachment(attachmentId);
      uploadedAttachmentSessionsRef.current.delete(attachmentId);
    },
    [captureWriteAccess, t],
  );

  const createRuntimeStreamResponse = useCallback(
    (chat: ConsoleChat, signal?: AbortSignal): Response => {
      const authRevision = getHostAuthContext().revision;
      const streamNavigation = navigationRef.current.version;
      const streamController = new AbortController();
      runtimeStreamAbortRef.current?.abort();
      runtimeStreamAbortRef.current = streamController;

      const isCurrentStream = () =>
        runtimeStreamAbortRef.current === streamController &&
        getHostAuthContext().revision === authRevision &&
        navigationRef.current.version === streamNavigation &&
        !streamController.signal.aborted &&
        stateRef.current.session?.id === chat.session_id;
      const forwardAbort = () => streamController.abort();
      signal?.addEventListener('abort', forwardAbort, { once: true });
      const encoder = new TextEncoder();
      const steerMessageIds = new Set<string>();

      let cancelRuntimeBridge = () => streamController.abort();
      return new Response(
        new ReadableStream<Uint8Array>({
          start: (responseStream) => {
            const release = () => {
              signal?.removeEventListener('abort', forwardAbort);
            };
            const settlement = createStreamSettlementGuard(release);
            cancelRuntimeBridge = () => {
              if (settlement.settle() && isCurrentStream()) setConnectionStatus('aborted');
              streamController.abort();
            };
            const close = (status: ConsoleConnectionStatus) => {
              if (!settlement.settle()) return;
              if (isCurrentStream()) setConnectionStatus(status);
              responseStream.close();
            };
            const fail = (streamError: unknown) => {
              if (!settlement.settle()) return;
              if (isCurrentStream()) {
                setError(errorMessage(streamError));
                setConnectionStatus('error');
              }
              responseStream.error(streamError);
            };
            let reconnectAttempt = 0;
            const subscribe = () => {
              if (isCurrentStream()) {
                ensureArtifactScope(chat.session_id);
                void artifactController.refresh();
              }
              const latest = stateRef.current.projections.find(
                ({ chat: item }) => item.id === chat.id,
              );
              const afterSequenceNumber = latest?.lastSequenceNumber ?? chat.last_sequence_number;
              void subscribeChatEvents({
                sessionId: chat.session_id,
                chatId: chat.id,
                afterSequenceNumber,
                signal: streamController.signal,
                onEvent: (event) => {
                  if (settlement.isSettled() || !isCurrentStream()) return;
                  if (event.sequence_number > afterSequenceNumber) reconnectAttempt = 0;
                  applyEvent(event);
                  if (shouldForwardRuntimeStreamEvent(event, steerMessageIds)) {
                    responseStream.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
                  }
                },
              })
                .then((result) => {
                  if (!isCurrentStream()) {
                    close('aborted');
                    return;
                  }
                  const current = stateRef.current.projections.find(
                    ({ chat: item }) => item.id === chat.id,
                  );
                  const action = selectChatStreamCompletionAction(
                    result.reason,
                    current?.chat.status ?? null,
                  );
                  if (action === 'aborted') {
                    close('aborted');
                  } else if (action === 'terminal') {
                    void artifactController.refresh(true);
                    close('eof');
                  } else if (action === 'reconnect') {
                    setConnectionStatus('connecting');
                    void waitForStreamReconnect(reconnectAttempt++, streamController.signal).then(
                      (retry) => {
                        if (retry && !settlement.isSettled() && isCurrentStream()) subscribe();
                        else if (!settlement.isSettled()) close('aborted');
                      },
                    );
                  } else {
                    fail(new Error(t('console.idleStreamEnded')));
                  }
                })
                .catch((streamError) => {
                  if (streamController.signal.aborted) {
                    close('aborted');
                    return;
                  }
                  fail(streamError);
                });
            };

            const projection = stateRef.current.projections.find(
              ({ chat: item }) => item.id === chat.id,
            );
            const bootstrap = createRuntimeStreamBootstrapEvent(
              projection?.chat ?? chat,
              projection?.responseId,
            );
            responseStream.enqueue(encoder.encode(`data: ${JSON.stringify(bootstrap)}\n\n`));
            subscribe();
          },
          cancel: () => {
            cancelRuntimeBridge();
          },
        }),
        {
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
        },
      );
    },
    [applyEvent, t, ensureArtifactScope, artifactController],
  );

  const ensureModelConfiguration = useCallback(
    async (signal?: AbortSignal) => {
      const assertAccess = captureWriteAccess();
      const epoch = sessionEpochRef.current;
      const assertCurrent = () => {
        assertAccess();
        if (signal?.aborted || epoch !== sessionEpochRef.current)
          throw new DOMException('Session changed', 'AbortError');
      };
      try {
        await checkModelConfiguration(
          {
            active: async () => (await dataConsoleApi.getActiveModels()).active_models,
            providers: async () => (await dataConsoleApi.getModelProviders()).providers,
            context: () => modelConfigApi.get(signal),
          },
          assertCurrent,
        );
        setModelConfigurationRequired(false);
        setError(null);
        return createModelConfigurationAdmission(`${getHostAuthContext().revision}:${epoch}`);
      } catch (cause) {
        assertCurrent();
        const incomplete = cause instanceof ModelConfigurationRequiredError;
        setModelConfigurationRequired(incomplete);
        setError(t(incomplete ? 'agent.cm.gateMissing' : 'agent.cm.gateError'));
        throw cause;
      }
    },
    [captureWriteAccess, t],
  );

  const submitRuntimeRequest = useCallback(
    async (
      text: string,
      signal?: AbortSignal,
      attachmentIds: string[] = [],
      artifactComments: ArtifactComment[] = [],
      admission?: ModelConfigurationAdmission,
    ): Promise<Response> => {
      const assertAccess = captureWriteAccess();
      const datasetNames = [...selectedDatasetNamesRef.current];
      const epoch = sessionEpochRef.current;
      const assertCurrentRequest = () => {
        assertAccess();
        if (signal?.aborted || epoch !== sessionEpochRef.current) {
          throw new DOMException('Session changed', 'AbortError');
        }
      };
      assertCurrentRequest();
      if (!admission?.consume(`${getHostAuthContext().revision}:${epoch}`)) {
        await ensureModelConfiguration(signal);
      }
      assertCurrentRequest();
      setConnectionStatus('connecting');
      const session = stateRef.current.session ?? (await createRuntimeSession(text));
      assertCurrentRequest();
      const datasourceId = selectedDatasourceIdRef.current;

      let chat: ConsoleChat;
      try {
        ({ chat } = await dataConsoleApi.createChat({
          session_id: session.id,
          text,
          datasource_id: datasourceId,
          dataset_names: datasourceId && datasetNames.length ? datasetNames : null,
          ...serializeRuntimeAttachmentIds(attachmentIds),
          artifact_comments: artifactComments,
        }));
      } catch (requestError) {
        assertCurrentRequest();
        const requiresModelConfiguration = isModelConfigurationError(requestError);
        setModelConfigurationRequired(requiresModelConfiguration);
        setError(
          requiresModelConfiguration
            ? t('console.modelConfigurationRequired')
            : errorMessage(requestError),
        );
        setConnectionStatus('error');
        throw requestError;
      }
      assertCurrentRequest();
      clearAcceptedRuntimeInputDraft(session.id, text);
      replaceChat(chat);
      setConnectionStatus('connecting');
      setError(null);
      setModelConfigurationRequired(false);
      return createRuntimeStreamResponse(chat, signal);
    },
    [
      createRuntimeSession,
      createRuntimeStreamResponse,
      replaceChat,
      t,
      captureWriteAccess,
      ensureModelConfiguration,
    ],
  );

  const reconnectRuntimeRequest = useCallback(
    async (sessionId: string, signal?: AbortSignal): Promise<Response> => {
      const current = stateRef.current;
      const session = current.session;
      const projection = current.projections.find(({ chat }) => chat.id === current.currentChatId);
      if (!session || session.id !== sessionId || !projection) {
        return new Response(
          new ReadableStream({
            start: (stream) => stream.close(),
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          },
        );
      }

      setConnectionStatus('connecting');
      setError(null);
      return createRuntimeStreamResponse(projection.chat, signal);
    },
    [createRuntimeStreamResponse],
  );

  const isCurrentChatRunning = useCallback(async (): Promise<boolean> => {
    const authRevision = getHostAuthContext().revision;
    const epoch = sessionEpochRef.current;
    const navigationVersion = navigationRef.current.version;
    const isCurrent = () =>
      getHostAuthContext().revision === authRevision &&
      epoch === sessionEpochRef.current &&
      navigationVersion === navigationRef.current.version;
    const current = stateRef.current;
    const session = current.session;
    const chatId = current.currentChatId;
    const projection = current.projections.find(({ chat }) => chat.id === chatId);
    if (!session || !chatId || !projection) return false;

    try {
      const status = await sessionStatusCache.get(
        `${epoch}:${navigationVersion}:${session.id}`,
        async () => (await dataConsoleApi.getSession(session.id)).session,
      );
      if (!isCurrent()) return true;
      // The backend computes status from active Chats, including the final
      // response/commit window. Fetch history only to recover a stale local stream.
      const snapshot =
        status.status !== 'running' && projection.chat.status === 'running'
          ? await dataConsoleApi.getSnapshot(session.id)
          : null;
      if (!isCurrent()) return true;
      const refreshedChat = snapshot?.chats.find((chat) => chat.id === chatId);
      const snapshotRunning = snapshot
        ? isSnapshotSessionRunning(snapshot)
        : status.status === 'running';

      // Ignore a late poll after the user has already switched sessions/chats.
      const latest = stateRef.current;
      if (latest.session?.id !== session.id || latest.currentChatId !== chatId) {
        return latest.projections.some(
          ({ chat }) => chat.id === latest.currentChatId && chat.status === 'running',
        );
      }

      // Recover a stale local running chat only after the server is idle.
      // The lightweight status above protects the terminal-event commit window.
      if (refreshedChat && refreshedChat.status !== projection.chat.status) {
        replaceChat(refreshedChat);
      }
      return snapshotRunning;
    } catch {
      // A failed status check must not create concurrent Chats. The queue will
      // retry this check on its normal polling interval.
      return true;
    }
  }, [replaceChat, sessionStatusCache]);

  const stopCurrentChat = useCallback(() => {
    let assertAccess: () => void;
    try {
      assertAccess = captureWriteAccess();
    } catch {
      return;
    }
    const projection = stateRef.current.projections.find(
      ({ chat }) => chat.id === stateRef.current.currentChatId,
    );
    if (!projection || projection.chat.status !== 'running') return;
    // Keep the SSE open: the runtime cancels asynchronously and the final
    // `response.cancelled` event drives the UI to the terminal state.
    userAbortedChatIdsRef.current.add(projection.chat.id);
    refreshConfirmation((version) => version + 1);
    void dataConsoleApi
      .stopChat(projection.chat.session_id, projection.chat.id)
      .then(({ chat }) => {
        assertAccess();
        // A still-running chat is settled by the ongoing SSE; replacing it here
        // would roll the event watermark back and replay events. Only adopt the
        // returned chat when the stop already produced a terminal state (orphan
        // path with an expired heartbeat).
        if (chat.status !== 'running') replaceChat(chat);
      })
      .catch((stopError) => {
        try {
          assertAccess();
        } catch {
          return;
        }
        if (stateRef.current.session?.id !== projection.chat.session_id) return;
        setError(errorMessage(stopError));
        setConnectionStatus('error');
      });
  }, [replaceChat, captureWriteAccess]);

  const answerToolConfirmation = useCallback(
    async (chatId: string, callId: string, decision: ToolConfirmationDecision) => {
      const assertAccess = captureWriteAccess();
      const sessionId = stateRef.current.session?.id;
      if (!sessionId) return;
      const isCurrent = () => {
        try {
          assertAccess();
          return stateRef.current.session?.id === sessionId;
        } catch {
          return false;
        }
      };
      const submitted = await toolConfirmationSubmissions.submit(
        { sessionId, chatId, callId },
        decision,
        {
          isCurrent,
          assertEligible: () => {
            assertAccess();
            const projection = stateRef.current.projections.find(
              ({ chat }) => chat.id === chatId && chat.session_id === sessionId,
            );
            const candidate =
              projection &&
              selectToolConfirmations(projection.messages, chatId).find(
                (item) => item.callId === callId,
              );
            if (
              !projection ||
              !candidate ||
              stateRef.current.currentChatId !== chatId ||
              toolConfirmationAvailability(candidate, {
                status: projection.chat.status,
                readOnly: false,
                stopping: userAbortedChatIdsRef.current.has(chatId),
                ready: !confirmationTracePendingRef.current.has(chatId),
              }) !== 'pending'
            )
              throw new Error(t('console.chatNotAwaitingConfirmation'));
          },
        },
        () =>
          dataConsoleApi.answerToolConfirmation(sessionId, chatId, { call_id: callId, decision }),
      );
      if (!submitted || !isCurrent()) return;
      // Refresh once after an answer, without replacing a running snapshot or its SSE watermark.
      // The ordinary history queue dedupes loaded chats, so use a fresh raw trace here.
      try {
        const [trace, snapshot] = await Promise.all([
          dataConsoleApi.getTrace(sessionId, chatId, 'raw'),
          dataConsoleApi.getSnapshot(sessionId),
        ]);
        if (!isCurrent()) return;
        const current = stateRef.current;
        const terminal = snapshot.chats.find(
          (chat) =>
            chat.id === chatId &&
            chat.session_id === sessionId &&
            chat.status !== 'running' &&
            chat.status !== 'created',
        );
        const next = {
          ...current,
          projections: current.projections.map((projection) => {
            if (projection.chat.id !== chatId) return projection;
            const hydrated = hydrateProjectionTrace(
              projection,
              trace.messages,
              trace.timing_events,
            );
            return terminal ? { ...hydrated, chat: terminal } : hydrated;
          }),
        };
        stateRef.current = next;
        setState(next);
      } catch {
        /* Keep the accepted/unknown lock; the existing stream still owns settlement. */
      }
    },
    [captureWriteAccess, t, toolConfirmationSubmissions],
  );

  const answerClarification = useCallback(
    async (chatId: string, clarificationId: string, result: AskUserQuestionToolResult) => {
      const assertAccess = captureWriteAccess();
      const projection = stateRef.current.projections.find(({ chat }) => chat.id === chatId);
      const pending =
        projection && projection.chat.status === 'running'
          ? selectPendingAskUserQuestion(projection.messages)
          : null;
      if (!projection || pending?.id !== clarificationId) {
        throw new Error(t('console.chatNotAwaitingConfirmation'));
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
        assertAccess();
        if (response.chat.id !== chatId) {
          throw new Error(t('console.clarificationChatMismatch'));
        }
      } catch (answerError) {
        try {
          assertAccess();
          setError(errorMessage(answerError));
        } catch {
          /* Ignore stale errors. */
        }
        throw answerError;
      }
    },
    [t, captureWriteAccess],
  );

  const currentProjection =
    state.projections.find(({ chat }) => chat.id === state.currentChatId) ?? null;
  const currentTimeline = useMemo(
    () => (currentProjection ? selectChatTimeline(currentProjection) : []),
    [currentProjection],
  );
  const [buildRuntimeSession] = useState(createRuntimeSessionBuilder);
  const runtimeSession = useMemo(
    () =>
      state.session
        ? buildRuntimeSession(state.session, state.projections)
        : {
            ...draftRuntimeSession,
            id: requestedSessionId || draftRuntimeSession.id,
            name: t('console.newSession'),
          },
    [requestedSessionId, state.projections, state.session, t, buildRuntimeSession],
  );

  const startNewSession = useCallback(() => {
    artifactController.ensureScope(null);
    setUiSession(null);
    historyRequestRef.current?.abort();
    historyRequestRef.current = null;
    historyCursorRef.current = null;
    setHistoryCursorValue(null);
    setHistoryLoading(false);
    setHistoryError(null);
    sessionEpochRef.current += 1;
    sessionLoadAbortRef.current?.abort();
    runtimeSessionCreationRef.current = null;
    runtimeStreamAbortRef.current?.abort();
    runtimeStreamAbortRef.current = null;

    const next: ConsoleState = {
      session: null,
      projections: [],
      currentChatId: null,
      initialHistoricalSegmentKeys: EMPTY_HISTORICAL_SEGMENT_KEYS,
    };
    stateRef.current = next;
    setState(next);
    setDraftQueueSessionId(createDraftQueueSessionId());
    setDraftEnableClarification(true);
    selectedDatasourceIdRef.current = null;
    setSelectedDatasourceId(null);
    selectDatasets([]);
    setError(null);
    setModelConfigurationRequired(false);
    setConnectionStatus('idle');
  }, [selectDatasets, artifactController]);

  const reloadSessions = useCallback(() => {
    reloadSessionCatalogRef.current();
  }, []);

  const forkChat = useCallback(
    async (chatId: string) => {
      const session = stateRef.current.session;
      if (!session) throw new Error(t('runtime.chatNotLoaded'));
      const assertAccess = captureWriteAccess(session.id);
      const { session: forked } = await dataConsoleApi.forkSession(session.id, chatId);
      assertAccess();
      accessRef.current.recordCreated(forked.id);
      refreshAccess((version) => version + 1);
      reloadSessions();
      onSessionResolvedRef.current?.(forked);
    },
    [reloadSessions, t, captureWriteAccess],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string): Promise<ConsoleSession> => {
      const assertAccess = captureWriteAccess(sessionId);
      const { session } = await dataConsoleApi.updateSession(sessionId, { title });
      assertAccess();
      setSessions((current) =>
        matchesSidebarQuery(session, sessionSearchTextRef.current)
          ? current.map((item) => (item.id === session.id ? session : item))
          : current.filter((item) => item.id !== session.id),
      );
      if (stateRef.current.session?.id === session.id) {
        const next = {
          ...stateRef.current,
          session,
        };
        stateRef.current = next;
        setState(next);
      }
      return session;
    },
    [captureWriteAccess],
  );

  const updateShareScope = useCallback(
    async (sessionId: string, scope: SessionShareScope) => {
      const assertAccess = captureWriteAccess(sessionId);
      const { session } = await dataConsoleApi.updateSession(sessionId, { share_scope: scope });
      assertAccess();
      if (session.share_scope !== scope) throw new Error(t('sessionShare.notSupported'));
      setSessions((items) => items.map((item) => (item.id === sessionId ? session : item)));
      if (stateRef.current.session?.id === sessionId) {
        const next = { ...stateRef.current, session };
        stateRef.current = next;
        setState(next);
      }
      return session;
    },
    [captureWriteAccess, t],
  );

  const setEnableClarification = useCallback(
    async (enableClarification: boolean) => {
      const assertAccess = captureWriteAccess();
      // Attachment uploads can already be creating the draft's backend Session.
      if (runtimeSessionCreationRef.current) {
        await runtimeSessionCreationRef.current;
        assertAccess();
      }
      const session = stateRef.current.session;
      if (!session) {
        setDraftEnableClarification(enableClarification);
        return;
      }
      const { session: updated } = await dataConsoleApi.updateSession(session.id, {
        enable_clarification: enableClarification,
      });
      assertAccess();
      setSessions((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      if (stateRef.current.session?.id !== updated.id) return;
      const next = {
        ...stateRef.current,
        session: updated,
        projections: withRunningChatClarification(
          stateRef.current.projections,
          updated.enable_clarification,
        ),
      };
      stateRef.current = next;
      setState(next);
    },
    [captureWriteAccess],
  );

  const deleteSession = useCallback(
    async (sessionId: string): Promise<void> => {
      const assertAccess = captureWriteAccess(sessionId);
      await dataConsoleApi.deleteSession(sessionId);
      assertAccess();
      accessRef.current.forget(sessionId);
      refreshAccess((version) => version + 1);
      setSessions((current) => current.filter((item) => item.id !== sessionId));
      setSessionsTotal((total) => Math.max(0, total - 1));
      if (stateRef.current.session?.id === sessionId) {
        startNewSession();
      }
    },
    [startNewSession, captureWriteAccess],
  );

  const submitFeedback = useCallback(
    async (
      chatId: string,
      kind: 'like' | 'dislike' | 'copy',
      detail?: { reason?: string | null; detail?: string | null },
    ) => {
      captureWriteAccess();
      const session = stateRef.current.session;
      if (!session) throw new Error(t('runtime.sessionNotLoaded'));
      await dataConsoleApi.createFeedback(session.id, chatId, { kind, ...detail });
      if (kind !== 'copy') reactionCache.set(`${session.id}:${chatId}`, kind);
    },
    [t, captureWriteAccess, reactionCache],
  );

  const getFeedbackReaction = useCallback(
    async (chatId: string): Promise<'like' | 'dislike' | null> => {
      const session = stateRef.current.session;
      if (!session) throw new Error(t('runtime.sessionNotLoaded'));
      return reactionCache.get(`${session.id}:${chatId}`, async () => {
        const result = await dataConsoleApi.getFeedbackReaction(session.id, chatId);
        return result.feedback?.kind ?? null;
      });
    },
    [t, reactionCache],
  );

  const clearFeedbackReaction = useCallback(
    async (chatId: string): Promise<void> => {
      captureWriteAccess();
      const session = stateRef.current.session;
      if (!session) throw new Error(t('runtime.sessionNotLoaded'));
      await dataConsoleApi.clearFeedbackReaction(session.id, chatId);
      reactionCache.set(`${session.id}:${chatId}`, null);
    },
    [t, captureWriteAccess, reactionCache],
  );

  const steerCurrentChat = useCallback(
    async (text: string, artifactComments: ArtifactComment[] = []) => {
      const assertAccess = captureWriteAccess();
      const trimmed = text.trim();
      if (!trimmed) throw new Error(t('console.steerRequired'));
      const current = stateRef.current;
      const projection = current.projections.find(({ chat }) => chat.id === current.currentChatId);
      if (!current.session || !projection || projection.chat.status !== 'running') {
        throw new Error(t('console.noRunningChat'));
      }
      const sessionId = current.session.id;
      const chatId = projection.chat.id;
      steerArrival.remember(projection);
      const pending = steerArrival.wait(chatId);
      try {
        await dataConsoleApi.steer(sessionId, chatId, trimmed, artifactComments);
        assertAccess();
        const latest = stateRef.current.projections.find(({ chat }) => chat.id === chatId);
        if (!latest) {
          steerArrival.fail(chatId, new Error(t('console.noRunningChat')));
        } else {
          steerArrival.observe(latest);
        }
        await pending;
        assertAccess();
      } catch (error) {
        const failed = error instanceof Error ? error : new Error(t('runtime.steerFailed'));
        steerArrival.fail(chatId, failed);
        await pending.catch(() => undefined);
        if (error instanceof SteerChatEndedError) {
          throw new Error(t('console.steerChatEnded'));
        }
        if (error instanceof SteerArrivalAbortedError) {
          throw new Error(t('sessionShare.readOnly'));
        }
        throw failed;
      }
    },
    [t, captureWriteAccess, steerArrival],
  );

  const editPlan = useCallback(
    async (chatId: string, plan: Plan, reason?: string | null) => {
      const assertAccess = captureWriteAccess();
      const current = stateRef.current;
      const session = current.session;
      const projection = current.projections.find(({ chat }) => chat.id === chatId);
      if (!session || !projection) throw new Error(t('runtime.chatNotLoaded'));

      const response = await dataConsoleApi.editPlan(session.id, chatId, { plan, reason });
      assertAccess();
      const latest = stateRef.current;
      const projections = latest.projections.map((item) =>
        item.chat.id === chatId ? { ...item, chat: { ...item.chat, plan: response.plan } } : item,
      );
      const next = { ...latest, projections };
      stateRef.current = next;
      setState(next);
    },
    [t, captureWriteAccess],
  );

  return {
    sessionArtifacts:
      auth.ready &&
      (!navigationChanged || draftResolved) &&
      sessionArtifacts.scope?.revision === auth.revision &&
      sessionArtifacts.scope.epoch === sessionEpochRef.current &&
      sessionArtifacts.scope.sessionId === (requestedSessionId ?? state.session?.id)
        ? sessionArtifacts
        : emptySessionArtifacts(),
    refreshSessionArtifacts,
    readOnly,
    accessVersion: `${auth.revision}:${navigationVersion}`,
    captureWriteAccess,
    updateShareScope,
    session: state.session,
    initialHistoricalSegmentKeys: state.initialHistoricalSegmentKeys,
    inputQueueSessionId: draftQueueSessionId,
    inputSessionEpoch: sessionEpochRef.current,
    sessions,
    sessionsLoadingMore,
    sessionsHasMore: sessions.length < sessionsTotal,
    searchSessions,
    loadMoreSessions,
    projections: state.projections,
    currentChatId: state.currentChatId,
    currentProjection,
    currentTimeline,
    datasources,
    selectedDatasourceId,
    datasets,
    datasetsLoading,
    datasetsError,
    selectedDatasetNames,
    selectDatasets,
    reloadDatasets: () => setDatasetsVersion((version) => version + 1),
    runtimeSession,
    runtimeSnapshotVersion,
    historyVersion,
    ensureChatTrace,
    deferHistoryUpdates,
    hasMoreHistory: historyCursorValue !== null,
    historyLoading,
    historyError,
    loadOlderChats,
    connectionStatus,
    error,
    modelConfigurationRequired,
    ensureModelConfiguration,
    cmModelConfigurationRequired,
    checkCMModelConfiguration: cmModelGate.check,
    selectChat,
    selectDatasource,
    startNewSession,
    reloadSessions,
    renameSession,
    enableClarification: state.session?.enable_clarification ?? draftEnableClarification,
    setEnableClarification,
    deleteSession,
    forkChat,
    submitFeedback,
    getFeedbackReaction,
    clearFeedbackReaction,
    steerCurrentChat,
    editPlan,
    ensureSession,
    uploadAttachment,
    discardAttachment,
    submitRuntimeRequest,
    reconnectRuntimeRequest,
    isCurrentChatRunning,
    answerClarification,
    answerToolConfirmation,
    toolConfirmationSubmissions,
    toolConfirmationReady:
      Boolean(currentProjection) &&
      !confirmationTracePendingRef.current.has(currentProjection!.chat.id),
    toolConfirmationStopping: Boolean(
      currentProjection && userAbortedChatIdsRef.current.has(currentProjection.chat.id),
    ),
    stopCurrentChat,
  };
}
