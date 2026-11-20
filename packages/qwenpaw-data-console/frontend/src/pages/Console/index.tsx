import { getHostAuthContext } from '@/services/auth';
import {
  CloseOutlined,
  DiffOutlined,
  FileTextOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  LinkOutlined,
  MenuOutlined,
  DownloadOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import { Button, Modal } from '@agentscope-ai/design';
import { Alert, message, Space, Spin, Tag, Tooltip } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  KeyboardEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
} from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { dataConsoleApi } from '@/features/console/api/client';
import { skillDraftsApi } from '@/features/console/api/skillDrafts';
import type { ArtifactComment, ReportManifest } from '@/features/console/api/types';
import ArtifactPreview from '@/features/console/components/ArtifactPreview/index';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import CompareConfigPanel from '@/features/console/components/CompareConfigPanel';
import type { CompareCandidate } from '@/features/console/components/CompareConfigPanel';
import OverflowTooltip from '@/features/console/components/OverflowTooltip';
import RuntimeConversation from '@/features/console/components/RuntimeConversation/index';
import type { RuntimeConversationHandle } from '@/features/console/components/RuntimeConversation/index';
import SectionComparePanel from '@/features/console/components/SectionComparePanel';
import LineDiffPanel from '@/features/console/components/LineDiffPanel';
import SessionWorkspace from '@/features/console/components/SessionWorkspace/index';
import SessionShareModal from '@/features/console/components/SessionShareModal';
import { accessArtifact, shareArtifact } from '@/features/console/model/artifacts/artifacts';
import { artifactFromLink } from '@/features/console/model/artifacts/artifactLinks';
import {
  artifactCommentKey,
  artifactFromCommentPath,
  isArtifactCommentable,
  isArtifactSplitCommentPreview,
  type ArtifactCommentHighlight,
  type PendingArtifactComment,
} from '@/features/console/model/artifacts/artifactComments';
import { selectPendingAskUserQuestion } from '@/features/console/model/chat/askUserQuestion';
import {
  readReportManifest,
  reportManifestPath,
  sourceDocumentPath,
  REPORT_PRODUCT_DIR,
} from '@/features/console/model/report/reportArtifacts';
import { type CompareSelection } from '@/features/console/model/report/sectionDiff';
import { isComparable, isHtml } from '@/features/console/model/compare/families';
import { defaultReportSelection } from '@/features/console/model/compare/selection';
import {
  buildAgentConfigurationPath,
  buildConsoleSessionPath,
} from '@/shared/navigation/sessionNavigation';
import {
  appendReviewPrompt,
  reviewReportFilename,
  selectReviewReport,
} from '@/features/console/model/chat/reviewSession';
import {
  cacheRuntimeInputAttachments,
  cacheRuntimeInputDraft,
  getRuntimeInputDraft,
} from '@/features/console/model/chat/runtimeInputCache';
import type { Artifact, ConversationTurn, WorkspaceTab } from '@/features/console/types';
import sharedStyles from '@/features/console/index.module.less';
import { mergeStyleModules } from '@/features/console/styleModules';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

let settingsComments: {
  sessionId: string;
  revision: number;
  comments: PendingArtifactComment[];
} | null = null;

const DEFAULT_WORKSPACE_WIDTH = 440;
const MIN_WORKSPACE_WIDTH = 340;
const MIN_CONVERSATION_WIDTH = 640;
const WORKSPACE_RESIZE_STEP = 16;

interface WorkspaceResizeState {
  pointerId: number;
  startX: number;
  startWidth: number;
  currentWidth: number;
}

// Give every compare candidate a label that stands alone in the picker. A name
// that is already unique (e.g. `report-v1.html`, a section title) is kept as
// is; duplicated bare file names — several `draft.md` from different sections
// or chats — are expanded with their parent path segments (`section-1/draft.md`,
// then `baseline/section-1/draft.md`, …) until each label is unique within its
// group.
function withUniqueCandidateNames(candidates: CompareCandidate[]): CompareCandidate[] {
  const groups = new Map<string, CompareCandidate[]>();
  candidates.forEach((candidate) => {
    const bucket = groups.get(candidate.name);
    if (bucket) bucket.push(candidate);
    else groups.set(candidate.name, [candidate]);
  });
  const nameByPath = new Map<string, string>();
  const segmentsOf = (path: string) => path.split('/').filter(Boolean);
  groups.forEach((group, name) => {
    if (group.length === 1) {
      nameByPath.set(group[0].path, name);
      return;
    }
    const maxDepth = Math.max(...group.map((item) => segmentsOf(item.path).length));
    let depth = 2;
    let labels = group.map((item) => segmentsOf(item.path).slice(-depth).join('/'));
    while (new Set(labels).size !== labels.length && depth < maxDepth) {
      depth += 1;
      labels = group.map((item) => segmentsOf(item.path).slice(-depth).join('/'));
    }
    group.forEach((item, index) => nameByPath.set(item.path, labels[index]));
  });
  return candidates.map((candidate) => ({
    ...candidate,
    name: nameByPath.get(candidate.path) ?? candidate.name,
  }));
}

function ConsoleHeader({
  readOnly,
  onShare,
  title,
  channel,
  onOpenNav,
  isReview,
  onReview,
  reviewing,
}: {
  readOnly: boolean;
  onShare?: () => void;
  title: string;
  channel?: string;
  onOpenNav: () => void;
  isReview: boolean;
  onReview?: () => void;
  reviewing: boolean;
}) {
  const { t } = useAppI18n();
  return (
    <header className={styles.consoleHeader}>
      <div className={styles.headerLeft}>
        <Button
          className={styles.mobileMenu}
          type="text"
          icon={<MenuOutlined />}
          aria-label={t('nav.open')}
          onClick={onOpenNav}
        />
        {channel && channel !== 'console' ? (
          <span className={styles.headerChannelIcon} aria-hidden="true">
            <ChannelIcon channel={channel} size={18} />
          </span>
        ) : null}
        <div className={styles.sessionTitle}>
          <OverflowTooltip title={title}>
            <strong>{title}</strong>
          </OverflowTooltip>
        </div>
        {readOnly ? <Tag>{t('sessionShare.readOnly')}</Tag> : null}
        {isReview ? <span className={styles.reviewBadge}>{t('review.session')}</span> : null}
      </div>
      <Space>
        {onShare ? <Button onClick={onShare}>{t('sessionShare.title')}</Button> : null}
        {onReview ? (
          <Tooltip title={t('review.startHint')}>
            <Button loading={reviewing} onClick={onReview}>
              {t('review.start')}
            </Button>
          </Tooltip>
        ) : null}
      </Space>
    </header>
  );
}

export default function ConsolePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { consoleController, openMobileNav } = useConsoleLayout();
  const { language, t } = useAppI18n();
  const { readOnly, captureWriteAccess, accessVersion } = consoleController;
  const refreshConfigurationOnEntry = useRef(consoleController.modelConfigurationRequired);
  const ensureModelConfiguration = consoleController.ensureModelConfiguration;
  useEffect(() => {
    if (!refreshConfigurationOnEntry.current || readOnly) return;
    const controller = new AbortController();
    void ensureModelConfiguration(controller.signal)
      .catch(() => undefined)
      .finally(() => {
        if (!controller.signal.aborted) refreshConfigurationOnEntry.current = false;
      });
    return () => controller.abort();
  }, [ensureModelConfiguration, readOnly]);
  const [shareOpen, setShareOpen] = useState(false);
  useEffect(() => {
    setShareOpen(false);
  }, [readOnly, accessVersion]);
  const requestedSessionId = searchParams.get('session_id');
  const activeSessionIdRef = useRef(requestedSessionId);
  activeSessionIdRef.current = requestedSessionId;
  const reviewOperationRef = useRef(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  useEffect(
    () => () => {
      activeSessionIdRef.current = null;
    },
    [],
  );
  const reviewReport = useMemo(
    () =>
      selectReviewReport(
        consoleController.session,
        consoleController.projections.map(({ chat }) => chat),
      ),
    [consoleController.session, consoleController.projections],
  );

  const handleStartReview = async () => {
    const source = consoleController.session;
    if (readOnly || !source || source.agent_id === 'review' || reviewOperationRef.current) return;
    reviewOperationRef.current = true;
    setReviewBusy(true);
    try {
      const assertAccess = captureWriteAccess(source.id);
      const { session } = await dataConsoleApi.createSession({
        agent_id: 'review',
        parent_session_id: source.id,
        title: t('review.title', { title: source.title || 'Data Console' }),
      });
      assertAccess();
      cacheRuntimeInputDraft(session.id, t('review.prompt', { sessionId: source.id }));
      consoleController.reloadSessions();
      if (activeSessionIdRef.current === source.id) navigate(buildConsoleSessionPath(session.id));
    } catch (error) {
      message.error(
        t('review.createFailed', {
          message: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      reviewOperationRef.current = false;
      setReviewBusy(false);
    }
  };

  const handleReturnReview = async () => {
    const source = consoleController.session;
    const report = reviewReport;
    if (readOnly || !source?.parent_session_id || !report || reviewOperationRef.current) return;
    const parentId = source.parent_session_id;
    reviewOperationRef.current = true;
    setReviewBusy(true);
    try {
      const assertAccess = captureWriteAccess(source.id);
      const assertParentAccess = captureWriteAccess(parentId);
      // Validate access before uploading; the report attachment must belong to the parent.
      await dataConsoleApi.getSession(parentId);
      const response = await dataConsoleApi.getWorkspaceFile(source.id, report.path, 'download');
      const file = new File([await response.blob()], reviewReportFilename(crypto.randomUUID()), {
        type: 'text/markdown',
      });
      assertAccess();
      assertParentAccess();
      const { attachment } = await dataConsoleApi.uploadAttachment(parentId, file);
      assertAccess();
      assertParentAccess();
      cacheRuntimeInputDraft(
        parentId,
        appendReviewPrompt(getRuntimeInputDraft(parentId), t('review.returnPrompt')),
      );
      cacheRuntimeInputAttachments(parentId, [attachment]);
      if (activeSessionIdRef.current === source.id) navigate(buildConsoleSessionPath(parentId));
    } catch (error) {
      message.error(
        t('review.returnFailed', {
          message: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      reviewOperationRef.current = false;
      setReviewBusy(false);
    }
  };
  const isHistoricalSessionLoading =
    Boolean(requestedSessionId) &&
    consoleController.session?.id !== requestedSessionId &&
    consoleController.connectionStatus !== 'error';
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('turns');
  const [settlementRefreshKey, setSettlementRefreshKey] = useState(0);
  const [skillDraftRefreshKey, setSkillDraftRefreshKey] = useState(0);
  const [previewSelection, setPreviewSelection] = useState<{
    artifact: Artifact;
    sessionId: string;
    accessVersion: string;
  } | null>(null);
  const previewArtifact =
    previewSelection?.sessionId === (requestedSessionId ?? consoleController.session?.id) &&
    previewSelection?.accessVersion === accessVersion
      ? previewSelection.artifact
      : null;
  const [previewHighlight, setPreviewHighlight] = useState<ArtifactCommentHighlight | null>(null);
  const [isArtifactCommentMode, setIsArtifactCommentMode] = useState(false);
  const [pendingComments, setPendingComments] = useState<PendingArtifactComment[]>(() => {
    const saved = settingsComments;
    return saved?.sessionId === requestedSessionId &&
      saved.revision === getHostAuthContext().revision
      ? saved.comments
      : [];
  });
  const [workspaceWidth, setWorkspaceWidth] = useState(DEFAULT_WORKSPACE_WIDTH);
  const [workspaceMaxWidth, setWorkspaceMaxWidth] = useState(DEFAULT_WORKSPACE_WIDTH);
  const [isWorkspaceCollapsed, setIsWorkspaceCollapsed] = useState(
    () => !new URLSearchParams(window.location.search).get('session_id'),
  );
  const [isWorkspaceResizing, setIsWorkspaceResizing] = useState(false);
  const [isReportSession, setIsReportSession] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  // The document picker lives in its own small modal; `compareOpen` swaps the
  // preview modal body for the two-pane diff once a pair is confirmed.
  const [comparePickerOpen, setComparePickerOpen] = useState(false);
  const [compareConfig, setCompareConfig] = useState<CompareSelection | null>(null);
  const [isCompareFullscreen, setIsCompareFullscreen] = useState(false);
  const [reportManifest, setReportManifest] = useState<ReportManifest | null>(null);
  const [reportManifestError, setReportManifestError] = useState<string | null>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const runtimeConversationRef = useRef<RuntimeConversationHandle>(null);
  const pendingCommentsAuthRevisionRef = useRef(getHostAuthContext().revision);
  const pendingCommentsSessionIdRef = useRef<string | null>(
    pendingComments.length ? requestedSessionId : null,
  );
  const workspaceResizeRef = useRef<WorkspaceResizeState | null>(null);
  const workspaceResizeCleanupRef = useRef<(() => void) | null>(null);
  const handleArtifactAccess = useCallback(
    async (artifact: Artifact, purpose: 'preview' | 'download') => {
      const sessionId = requestedSessionId ?? consoleController.session?.id;
      if (!sessionId) return;
      try {
        await accessArtifact(sessionId, artifact, purpose, dataConsoleApi);
      } catch (artifactError) {
        const message =
          artifactError instanceof Error ? artifactError.message : String(artifactError);
        window.alert(t('console.fileAccessFailed', { message }));
      }
    },
    [consoleController.session?.id, requestedSessionId, t],
  );

  const handleArtifactDownload = useCallback(
    (artifact: Artifact) => {
      void handleArtifactAccess(artifact, 'download');
    },
    [handleArtifactAccess],
  );

  const handleArtifactShare = useCallback(
    async (artifact: Artifact) => {
      const sessionId = consoleController.session?.id;
      if (!sessionId) {
        const error = new Error(t('artifact.shareUnavailable'));
        message.error(error.message);
        throw error;
      }

      try {
        if (!navigator.clipboard?.writeText) {
          throw new Error(t('artifact.clipboardUnavailable'));
        }
        const assertAccess = captureWriteAccess(sessionId);
        const shared = await shareArtifact(sessionId, artifact, dataConsoleApi);
        assertAccess();
        await navigator.clipboard.writeText(shared.url);
        message.success(t('artifact.linkCopied'));
      } catch (shareError) {
        const errorMessage = shareError instanceof Error ? shareError.message : String(shareError);
        message.error(t('artifact.shareFailed', { message: errorMessage }));
        throw shareError;
      }
    },
    [consoleController.session?.id, captureWriteAccess, t],
  );

  // Opening a comment chip re-enters the preview already scrolled to that line
  // range; a plain open always drops back to the regular render.
  const openArtifact = useCallback(
    (artifact: Artifact, highlight?: ArtifactCommentHighlight | null) => {
      const sessionId = requestedSessionId ?? consoleController.session?.id;
      if (!sessionId) return;
      setPreviewHighlight(highlight ?? null);
      setIsArtifactCommentMode(Boolean(highlight));
      setPreviewSelection({ artifact, sessionId, accessVersion });
    },
    [requestedSessionId, consoleController.session?.id, accessVersion],
  );

  const closeArtifact = useCallback(() => {
    setPreviewSelection(null);
    setPreviewHighlight(null);
    setIsArtifactCommentMode(false);
  }, []);

  useEffect(
    () =>
      uiRegistry.object('artifact', async (ref, context) => {
        if (ref.kind !== 'artifact' || ref.session_id !== consoleController.session?.id)
          throw new Error('FORBIDDEN');
        const artifact = consoleController.projections
          .flatMap(({ chat }) => chat.artifacts)
          .find((item) => item.id === ref.artifact_id);
        if (!artifact) throw new Error('TARGET_NOT_FOUND');
        context.assertCurrent();
        openArtifact({ ...artifactFromCommentPath(artifact.path), id: artifact.id });
        await uiRegistry.wait(() => isArtifactReady(ref) || isArtifactFailed(ref), context.signal);
        context.assertCurrent();
        if (!isArtifactReady(ref)) throw new Error('MODULE_NOT_READY');
        return { object_ref: ref, view_state: 'opened', completed_steps: ['preview_loaded'] };
      }),
    [consoleController.session?.id, consoleController.projections, openArtifact],
  );

  const handleArtifactLinkClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!(event.target instanceof Element)) return;
      const link = event.target.closest('a[href]');
      const artifact = artifactFromLink(link?.getAttribute('href') ?? null);
      if (!artifact) return;

      event.preventDefault();
      event.stopPropagation();
      openArtifact(artifact);
    },
    [openArtifact],
  );

  const handleOpenComment = useCallback(
    (item: ArtifactComment) => {
      openArtifact(artifactFromCommentPath(item.path), {
        lineStart: item.line_start,
        lineEnd: item.line_end,
        comment: item.comment,
      });
    },
    [openArtifact],
  );

  const handlePreviewComment = useCallback(
    (item: PendingArtifactComment) => {
      try {
        captureWriteAccess();
      } catch {
        return;
      }
      const key = artifactCommentKey(item);
      if (pendingComments.some((current) => artifactCommentKey(current) === key)) {
        closeArtifact();
        return;
      }
      pendingCommentsSessionIdRef.current = consoleController.session?.id ?? null;
      setPendingComments((current) => [...current, item]);
      closeArtifact();
    },
    [closeArtifact, consoleController.session?.id, pendingComments, captureWriteAccess],
  );

  const handleConsumePendingComments = useCallback(() => {
    pendingCommentsSessionIdRef.current = null;
    setPendingComments([]);
  }, []);

  const handleRestorePendingComments = useCallback(
    (items: readonly PendingArtifactComment[]) => {
      pendingCommentsSessionIdRef.current = consoleController.session?.id ?? null;
      setPendingComments((current) => {
        const knownKeys = new Set(current.map(artifactCommentKey));
        const restored = items.filter((item) => !knownKeys.has(artifactCommentKey(item)));
        return restored.length ? [...current, ...restored] : current;
      });
    },
    [consoleController.session?.id],
  );

  // The compare action shows up for any line-comparable artifact (markdown,
  // csv, sql, txt, json), not just a reproduction draft.
  const draftPath = previewArtifact?.access?.relativePath ?? previewArtifact?.path ?? null;
  const canCompare = Boolean(draftPath) && isComparable(draftPath ?? '');
  const isComparing = compareOpen && Boolean(compareConfig);
  const isSplitCommentPreview = Boolean(
    !isComparing &&
    previewArtifact &&
    isArtifactSplitCommentPreview(previewArtifact, !readOnly && isArtifactCommentMode),
  );
  const gtPath = reportManifest ? sourceDocumentPath(reportManifest) : null;

  // Documents the picker can choose from: every aggregated chat artifact plus the
  // report-reproduction products, filtered to the text-comparable kinds (方案7 Q2).
  const compareCandidates = useMemo<CompareCandidate[]>(() => {
    const seen = new Set<string>();
    const list: CompareCandidate[] = [];
    const push = (path: string | null | undefined, name?: string | null) => {
      if (!path || seen.has(path) || !isComparable(path)) return;
      seen.add(path);
      list.push({ path, name: name || path.split('/').pop() || path });
    };
    consoleController.projections.forEach((projection) => {
      projection.chat.artifacts.forEach((artifact) => push(artifact.path, artifact.name));
    });
    if (reportManifest) {
      push(gtPath, reportManifest.title || 'GT');
      reportManifest.sections.forEach(({ section }) => {
        push(`${REPORT_PRODUCT_DIR}/${section.source_path}`, section.title);
        push(`${REPORT_PRODUCT_DIR}/${section.draft_path}`, `${section.title} · draft`);
      });
    }
    push(draftPath, previewArtifact?.name);
    return withUniqueCandidateNames(list);
  }, [consoleController.projections, reportManifest, gtPath, draftPath, previewArtifact?.name]);

  // The pair pre-filled when the picker opens: this document against its GT.
  // The picker validates the family and drops the reference when it does not
  // match; recommendations come from the backend, not hardcoded here.
  const defaultCompareSelection = useMemo<CompareSelection>(
    () =>
      defaultReportSelection(
        reportManifest,
        draftPath ?? '',
        previewArtifact?.name || draftPath || '',
      ),
    [draftPath, reportManifest, previewArtifact?.name],
  );

  // Comparison lives inside the preview modal, so previewing anything else — or
  // closing the modal — always drops back to the plain artifact render.
  useEffect(() => {
    setCompareOpen(false);
    setComparePickerOpen(false);
    setCompareConfig(null);
    setIsCompareFullscreen(false);
  }, [previewArtifact]);

  const previousTurnsRef = useRef<ConversationTurn[]>([]);
  const turns = useMemo<ConversationTurn[]>(() => {
    const next: ConversationTurn[] = consoleController.projections
      .map((projection) => {
        const { chat } = projection;
        const datasourceName =
          consoleController.datasources.find((datasource) => datasource.id === chat.datasource_id)
            ?.name ??
          chat.datasource_id ??
          t('console.noDatasource');
        const sentAtDate = chat.started_at ? new Date(chat.started_at) : null;
        const sentAt =
          sentAtDate && !Number.isNaN(sentAtDate.getTime())
            ? sentAtDate.toLocaleTimeString(language === 'zh' ? 'zh-CN' : 'en-US', {
                hour: '2-digit',
                minute: '2-digit',
              })
            : '';

        const status: ConversationTurn['status'] =
          chat.status === 'running'
            ? selectPendingAskUserQuestion(projection.messages)
              ? 'awaiting'
              : 'running'
            : 'done';

        return {
          id: chat.id,
          number: String(chat.sequence).padStart(2, '0'),
          title: chat.user_input || t('console.turnTitle', { number: chat.sequence }),
          dataSource: datasourceName,
          sentAt,
          status,
        };
      })
      .sort((left, right) => Number(left.number) - Number(right.number));
    const previous = previousTurnsRef.current;
    if (
      previous.length === next.length &&
      next.every((turn, index) => {
        const old = previous[index];
        return (
          turn.id === old.id &&
          turn.number === old.number &&
          turn.title === old.title &&
          turn.dataSource === old.dataSource &&
          turn.sentAt === old.sentAt &&
          turn.status === old.status
        );
      })
    )
      return previous;
    previousTurnsRef.current = next;
    return next;
  }, [consoleController.datasources, consoleController.projections, language, t]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'QwenPaw Data Console';

    return () => {
      document.title = previousTitle;
    };
  }, []);

  useEffect(() => {
    // A route without session_id is the draft/new-conversation state. Keep its
    // workspace collapsed by default, while opening an existing session keeps
    // the workspace available immediately.
    setIsWorkspaceCollapsed(!searchParams.get('session_id'));
  }, [searchParams]);

  useEffect(() => {
    const ownerSessionId = pendingCommentsSessionIdRef.current;
    const revision = getHostAuthContext().revision;
    const identityChanged = pendingCommentsAuthRevisionRef.current !== revision;
    pendingCommentsAuthRevisionRef.current = revision;
    if (!identityChanged && (!ownerSessionId || ownerSessionId === requestedSessionId)) return;
    pendingCommentsSessionIdRef.current = null;
    setPendingComments([]);
  }, [requestedSessionId, consoleController.accessVersion]);

  useEffect(() => {
    settingsComments = null;
  }, []);

  // Switching sessions resets the compare/report UI so a new conversation never
  // inherits the previous one's diff pane or fullscreen state.
  useEffect(() => {
    setWorkspaceTab('turns');
    setIsReportSession(false);
    setCompareOpen(false);
    setComparePickerOpen(false);
    setCompareConfig(null);
    setIsCompareFullscreen(false);
  }, [consoleController.session?.id]);

  // Tool completion automatically registers new workspace files. Wait for that
  // signal instead of probing a nonexistent manifest in every ordinary chat.
  const hasReportManifest = consoleController.projections.some(
    (projection) =>
      projection.chat.session_id === consoleController.session?.id &&
      projection.chat.artifacts.some(
        (artifact) => artifact.path.replace(/^\.\//, '') === reportManifestPath(),
      ),
  );

  // Registered manifests can change while the report is being reproduced.
  // Keep refreshing their contents for the workspace and compare flow.
  useEffect(() => {
    const sessionId = consoleController.session?.id;
    setReportManifestError(null);
    setReportManifest(null);
    setIsReportSession(false);
    if (!sessionId || !hasReportManifest) {
      return undefined;
    }
    let disposed = false;
    const check = () => {
      void readReportManifest(sessionId)
        .then((manifest) => {
          if (disposed) return;
          // Reading the file is the whole test: whichever skill run wrote it made
          // this a report session, so no separate flag is needed.
          setIsReportSession(Boolean(manifest));
          setReportManifest(manifest);
          setReportManifestError(null);
        })
        .catch((error: unknown) => {
          if (!disposed)
            setReportManifestError(error instanceof Error ? error.message : String(error));
        });
    };
    check();
    const interval = window.setInterval(check, 30_000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [consoleController.session?.id, hasReportManifest]);

  const currentChatId = consoleController.currentChatId;
  const currentChatStatus = consoleController.currentProjection?.chat.status;
  const previousChatStateRef = useRef({
    chatId: currentChatId,
    status: currentChatStatus,
  });
  useEffect(() => {
    const previousChatState = previousChatStateRef.current;
    previousChatStateRef.current = {
      chatId: currentChatId,
      status: currentChatStatus,
    };
    const chatJustFinished =
      previousChatState.chatId === currentChatId &&
      previousChatState.status === 'running' &&
      (currentChatStatus === 'completed' ||
        currentChatStatus === 'failed' ||
        currentChatStatus === 'canceled');
    if (!chatJustFinished) return undefined;

    const timeoutId = window.setTimeout(() => {
      setSettlementRefreshKey((current) => current + 1);
    }, 1_500);
    return () => window.clearTimeout(timeoutId);
  }, [currentChatId, currentChatStatus]);

  useEffect(
    () => () => {
      workspaceResizeCleanupRef.current?.();
    },
    [],
  );

  useEffect(() => {
    const syncWorkspaceBounds = () => {
      const page = pageRef.current;
      const workspace = page?.querySelector<HTMLElement>('#session-workspace');

      if (!page || !workspace || workspace.offsetWidth === 0) {
        return;
      }

      const availableWidth = page.getBoundingClientRect().width;
      const maxWidth = Math.max(
        MIN_WORKSPACE_WIDTH,
        Math.min(Math.floor(availableWidth / 2), availableWidth - MIN_CONVERSATION_WIDTH - 24),
      );
      const currentWidth = workspace.getBoundingClientRect().width;
      const nextWidth = Math.min(maxWidth, Math.max(MIN_WORKSPACE_WIDTH, currentWidth));

      page.style.setProperty('--session-workspace-width', `${nextWidth}px`);
      setWorkspaceWidth(Math.round(nextWidth));
      setWorkspaceMaxWidth(maxWidth);
    };

    syncWorkspaceBounds();
    window.addEventListener('resize', syncWorkspaceBounds);

    return () => {
      window.removeEventListener('resize', syncWorkspaceBounds);
    };
  }, []);

  const selectChat = consoleController.selectChat;
  const handleTurnSelect = useCallback(
    (turn: ConversationTurn) => {
      selectChat(turn.id);
      runtimeConversationRef.current?.scrollToChat(turn.id);
    },
    [selectChat],
  );

  const getWorkspaceBounds = () => {
    const page = pageRef.current;

    if (!page) {
      return null;
    }

    const availableWidth = page.getBoundingClientRect().width;

    return {
      min: MIN_WORKSPACE_WIDTH,
      max: Math.max(
        MIN_WORKSPACE_WIDTH,
        Math.min(Math.floor(availableWidth / 2), availableWidth - MIN_CONVERSATION_WIDTH - 24),
      ),
    };
  };

  const applyWorkspaceWidth = (width: number) => {
    const bounds = getWorkspaceBounds();

    if (!bounds || !pageRef.current) {
      return workspaceWidth;
    }

    const nextWidth = Math.min(bounds.max, Math.max(bounds.min, width));
    pageRef.current.style.setProperty('--session-workspace-width', `${nextWidth}px`);
    setWorkspaceMaxWidth(bounds.max);

    return nextWidth;
  };

  const handleWorkspaceResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) {
      return;
    }

    const workspace = pageRef.current?.querySelector<HTMLElement>('#session-workspace');

    if (!workspace) {
      return;
    }

    const currentWidth = workspace.getBoundingClientRect().width;
    workspaceResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: currentWidth,
      currentWidth,
    };

    const handlePointerMove = (moveEvent: globalThis.PointerEvent) => {
      const resizeState = workspaceResizeRef.current;

      if (!resizeState || resizeState.pointerId !== moveEvent.pointerId) {
        return;
      }

      resizeState.currentWidth = applyWorkspaceWidth(
        resizeState.startWidth + resizeState.startX - moveEvent.clientX,
      );
    };

    const finishResize = (finishEvent: globalThis.PointerEvent) => {
      const resizeState = workspaceResizeRef.current;

      if (!resizeState || resizeState.pointerId !== finishEvent.pointerId) {
        return;
      }

      setWorkspaceWidth(Math.round(resizeState.currentWidth));
      workspaceResizeRef.current = null;
      setIsWorkspaceResizing(false);
      workspaceResizeCleanupRef.current?.();
    };

    workspaceResizeCleanupRef.current?.();
    workspaceResizeCleanupRef.current = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finishResize);
      window.removeEventListener('pointercancel', finishResize);
      workspaceResizeCleanupRef.current = null;
    };
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finishResize);
    window.addEventListener('pointercancel', finishResize);
    setIsWorkspaceResizing(true);
  };

  const handleWorkspaceResizeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const workspace = pageRef.current?.querySelector<HTMLElement>('#session-workspace');

    if (!workspace) {
      return;
    }

    const currentWidth = workspace.getBoundingClientRect().width;
    const bounds = getWorkspaceBounds();
    let nextWidth: number | null = null;

    if (event.key === 'ArrowLeft') {
      nextWidth = currentWidth + WORKSPACE_RESIZE_STEP;
    } else if (event.key === 'ArrowRight') {
      nextWidth = currentWidth - WORKSPACE_RESIZE_STEP;
    } else if (event.key === 'Home') {
      nextWidth = bounds?.min ?? MIN_WORKSPACE_WIDTH;
    } else if (event.key === 'End') {
      nextWidth = bounds?.max ?? workspaceMaxWidth;
    }

    if (nextWidth === null) {
      return;
    }

    event.preventDefault();
    setWorkspaceWidth(Math.round(applyWorkspaceWidth(nextWidth)));
  };

  const handleWorkspaceCollapseToggle = () => {
    workspaceResizeCleanupRef.current?.();
    workspaceResizeRef.current = null;
    setIsWorkspaceResizing(false);
    setIsWorkspaceCollapsed((current) => !current);
  };

  const handleRevealSettlementCards = useCallback(() => {
    workspaceResizeCleanupRef.current?.();
    workspaceResizeRef.current = null;
    setIsWorkspaceResizing(false);
    setIsWorkspaceCollapsed(false);
    setWorkspaceTab('settlement');
  }, []);

  const handleCreateSkillDraft = useCallback(
    async (chatId: string) => {
      const sessionId = consoleController.session?.id;
      if (!sessionId) {
        const error = new Error(t('skillDraft.createUnavailable'));
        message.error(error.message);
        throw error;
      }

      try {
        const assertAccess = captureWriteAccess(sessionId);
        await skillDraftsApi.create(sessionId, chatId);
        assertAccess();
        setSkillDraftRefreshKey((current) => current + 1);
        handleRevealSettlementCards();
        message.success(t('skillDraft.createStarted'));
      } catch (createError) {
        const errorMessage =
          createError instanceof Error ? createError.message : String(createError);
        message.error(t('skillDraft.createFailed', { message: errorMessage }));
        throw createError;
      }
    },
    [consoleController.session?.id, handleRevealSettlementCards, t, captureWriteAccess],
  );

  return (
    <div
      className={`${styles.consolePage} ${
        isWorkspaceResizing ? styles.workspaceResizing : ''
      } ${isWorkspaceCollapsed ? styles.workspaceCollapsed : ''}`}
      ref={pageRef}
      onWheelCapture={consoleController.deferHistoryUpdates}
      onTouchMoveCapture={consoleController.deferHistoryUpdates}
      aria-busy={isHistoricalSessionLoading}
      onClickCapture={handleArtifactLinkClick}
    >
      {isHistoricalSessionLoading ? (
        <div className={styles.historyLoading} role="status" aria-live="polite">
          <Spin size="large" />
          <span>{t('common.loading')}</span>
        </div>
      ) : null}
      <section className={styles.console}>
        <ConsoleHeader
          readOnly={readOnly}
          onShare={
            !readOnly && consoleController.session && !isHistoricalSessionLoading
              ? () => setShareOpen(true)
              : undefined
          }
          title={consoleController.session?.title || t('console.newSession')}
          channel={consoleController.session?.channel}
          onOpenNav={openMobileNav}
          isReview={consoleController.session?.agent_id === 'review'}
          reviewing={reviewBusy}
          onReview={
            !readOnly &&
            consoleController.session &&
            consoleController.session.agent_id !== 'review' &&
            consoleController.session.id === requestedSessionId
              ? handleStartReview
              : undefined
          }
        />
        <RuntimeConversation
          controller={consoleController}
          pendingComments={pendingComments}
          composerNotice={
            !readOnly && reviewReport ? (
              <div className={styles.reviewNotice}>
                <FileTextOutlined aria-hidden="true" />
                <span>{t('review.reportReady')}</span>
                <Button
                  type="primary"
                  size="small"
                  loading={reviewBusy}
                  onClick={handleReturnReview}
                >
                  {t('review.return')}
                </Button>
              </div>
            ) : undefined
          }
          onArtifactOpen={openArtifact}
          onArtifactDownload={handleArtifactDownload}
          onArtifactShare={readOnly ? undefined : handleArtifactShare}
          onCreateSkillDraft={readOnly ? undefined : handleCreateSkillDraft}
          onOpenAgentConfiguration={() => {
            const sessionId = consoleController.session?.id;
            settingsComments = sessionId
              ? { sessionId, revision: getHostAuthContext().revision, comments: pendingComments }
              : null;
            const query = new URLSearchParams(searchParams);
            if (sessionId) query.set('session_id', sessionId);
            const returnTo = `/console${query.size ? `?${query}` : ''}`;
            navigate(buildAgentConfigurationPath(returnTo));
          }}
          onOpenDataBridgeModelConfig={() => navigate(buildAgentConfigurationPath())}
          onOpenComment={handleOpenComment}
          onConsumePendingComments={handleConsumePendingComments}
          onRestorePendingComments={handleRestorePendingComments}
          ref={runtimeConversationRef}
        />
      </section>

      {shareOpen && !readOnly && consoleController.session ? (
        <SessionShareModal
          key={`${consoleController.session.id}:${accessVersion}`}
          session={consoleController.session}
          onClose={() => setShareOpen(false)}
          onSave={consoleController.updateShareScope}
        />
      ) : null}

      <SessionWorkspace
        sessionArtifacts={consoleController.sessionArtifacts}
        artifactSessionId={requestedSessionId ?? consoleController.session?.id}
        artifactAccessVersion={accessVersion}
        onRefreshSessionArtifacts={consoleController.refreshSessionArtifacts}
        historyLoading={consoleController.historyLoading}
        onLoadOlderTurns={
          consoleController.hasMoreHistory &&
          !consoleController.historyLoading &&
          !consoleController.historyError
            ? consoleController.loadOlderChats
            : undefined
        }
        readOnly={readOnly}
        activeTab={workspaceTab}
        selectedTurnId={consoleController.currentChatId ?? ''}
        turns={turns}
        chatProjection={consoleController.currentProjection}
        sessionId={consoleController.session?.id}
        isReportSession={isReportSession}
        sessionChannel={consoleController.session?.channel}
        settlementRefreshKey={settlementRefreshKey}
        skillDraftRefreshKey={skillDraftRefreshKey}
        onTabChange={setWorkspaceTab}
        onClose={handleWorkspaceCollapseToggle}
        onRevealSettlementCards={handleRevealSettlementCards}
        onTurnSelect={handleTurnSelect}
        onPlanEdit={consoleController.editPlan}
        onArtifactOpen={openArtifact}
        onArtifactDownload={handleArtifactDownload}
        onArtifactShare={readOnly ? undefined : handleArtifactShare}
      />
      <div
        className={styles.workspaceResizeHandle}
        role="separator"
        tabIndex={0}
        aria-controls="session-workspace"
        aria-label={t('console.resizeWorkspace')}
        aria-orientation="vertical"
        aria-valuemin={MIN_WORKSPACE_WIDTH}
        aria-valuemax={workspaceMaxWidth}
        aria-valuenow={workspaceWidth}
        aria-valuetext={t('console.workspacePixels', { width: workspaceWidth })}
        title={t('console.dragResizeWorkspace')}
        onKeyDown={handleWorkspaceResizeKeyDown}
        onPointerDown={handleWorkspaceResizeStart}
      />
      <button
        className={styles.workspaceCollapseHandle}
        type="button"
        aria-controls="session-workspace"
        aria-expanded={!isWorkspaceCollapsed}
        aria-label={
          isWorkspaceCollapsed ? t('console.expandWorkspace') : t('console.collapseWorkspace')
        }
        title={isWorkspaceCollapsed ? t('console.expandWorkspace') : t('console.collapseWorkspace')}
        onClick={handleWorkspaceCollapseToggle}
      >
        {isWorkspaceCollapsed ? <LeftOutlined /> : <RightOutlined />}
      </button>

      <Modal
        className={`${styles.artifactModal} ${
          isComparing ? styles.artifactModalCompare : ''
        } ${isCompareFullscreen ? styles.artifactModalFullscreen : ''}`}
        width={
          isComparing ? (isCompareFullscreen ? '100vw' : 1200) : isSplitCommentPreview ? 1200 : 960
        }
        styles={
          isCompareFullscreen
            ? {
                content: {
                  height: '100dvh',
                  boxSizing: 'border-box',
                  borderRadius: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  paddingBottom: 24,
                },
                header: { flexShrink: 0 },
                body: { flex: '1 1 0', minHeight: 0, maxHeight: 'none' },
                footer: { flexShrink: 0 },
              }
            : undefined
        }
        open={Boolean(previewArtifact)}
        closeIcon={
          isComparing ? (
            <span
              className={styles.compareModalActions}
              onClick={(event) => event.stopPropagation()}
            >
              <button
                type="button"
                className={styles.compareIconBtn}
                aria-label={
                  isCompareFullscreen
                    ? t('console.compareExitFullscreen')
                    : t('console.compareFullscreen')
                }
                title={
                  isCompareFullscreen
                    ? t('console.compareExitFullscreen')
                    : t('console.compareFullscreen')
                }
                onClick={() => setIsCompareFullscreen((value) => !value)}
              >
                {isCompareFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              </button>
              <button
                type="button"
                className={styles.compareIconBtn}
                aria-label={t('console.compareClose')}
                title={t('console.compareClose')}
                onClick={closeArtifact}
              >
                <CloseOutlined />
              </button>
            </span>
          ) : undefined
        }
        title={
          <div className={styles.artifactModalTitle}>
            <div className={styles.artifactModalFileTitle}>
              <strong
                title={
                  isComparing && compareConfig
                    ? `${compareConfig.referenceLabel} ↔ ${compareConfig.targetLabel}`
                    : previewArtifact?.name
                }
              >
                {isComparing && compareConfig
                  ? `${compareConfig.referenceLabel} ↔ ${compareConfig.targetLabel}`
                  : previewArtifact?.name}
              </strong>
              {!isComparing &&
              previewArtifact?.title &&
              previewArtifact.title !== previewArtifact.name ? (
                <span title={previewArtifact.title}>{previewArtifact.title}</span>
              ) : null}
            </div>
          </div>
        }
        footer={[
          ...(canCompare
            ? [
                <Button
                  key="compare"
                  type={isComparing ? 'primary' : 'default'}
                  ghost={isComparing}
                  icon={<DiffOutlined />}
                  onClick={() => {
                    // Compare and line-comment modes render mutually exclusive bodies.
                    setIsArtifactCommentMode(false);
                    setPreviewHighlight(null);
                    if (isComparing) {
                      // Leaving compare drops the picked config and the fullscreen
                      // flag so the next open starts fresh, not maximised.
                      setCompareOpen(false);
                      setCompareConfig(null);
                      setIsCompareFullscreen(false);
                    } else {
                      // Entering compare opens the small picker first; the target
                      // document is fixed to the artifact just clicked.
                      setComparePickerOpen(true);
                    }
                  }}
                >
                  {t(isComparing ? 'console.exitSourceCompare' : 'console.compareSourceReport')}
                </Button>,
              ]
            : []),
          !readOnly ? (
            <Button
              key="comment"
              icon={<FileTextOutlined />}
              type={isArtifactCommentMode ? 'primary' : 'default'}
              ghost={isArtifactCommentMode}
              disabled={isComparing || !previewArtifact || !isArtifactCommentable(previewArtifact)}
              onClick={() => {
                setPreviewHighlight(null);
                setIsArtifactCommentMode((current) => !current);
              }}
            >
              {isArtifactCommentMode
                ? t('artifact.commentExitMode')
                : t('console.commentSelectedRows')}
            </Button>
          ) : null,
          <Button
            key="download"
            icon={<DownloadOutlined />}
            onClick={() => {
              if (previewArtifact) void handleArtifactAccess(previewArtifact, 'download');
            }}
          >
            {t('console.download')}
          </Button>,
          <Button
            key="open"
            type="primary"
            icon={<LinkOutlined />}
            onClick={() => {
              if (previewArtifact) void handleArtifactAccess(previewArtifact, 'preview');
            }}
          >
            {t('console.openNewWindow')}
          </Button>,
        ]}
        onCancel={closeArtifact}
      >
        {previewArtifact && previewSelection ? (
          isComparing && compareConfig ? (
            // The rich numeric-highlight view serves the report-reproduction
            // draft↔GT pair (the only one with a persisted verdict) and any pair
            // with an HTML side, which needs rich rendering. Every other pair —
            // e.g. a plain markdown↔markdown or txt/csv/sql/json match — reads
            // best as a line-by-line diff.
            compareConfig.section ||
            isHtml(compareConfig.referencePath) ||
            isHtml(compareConfig.targetPath) ? (
              <SectionComparePanel
                sessionId={previewSelection.sessionId}
                selection={compareConfig}
              />
            ) : (
              <LineDiffPanel sessionId={previewSelection.sessionId} selection={compareConfig} />
            )
          ) : (
            <div
              className={`${styles.artifactPreviewContent} ${
                isSplitCommentPreview ? styles.artifactPreviewContentSplit : ''
              }`}
            >
              <ArtifactPreview
                artifact={previewArtifact}
                commentMode={!readOnly && isArtifactCommentMode}
                highlight={previewHighlight}
                onComment={readOnly ? undefined : handlePreviewComment}
                sessionId={previewSelection.sessionId}
              />
            </div>
          )
        ) : null}
      </Modal>

      <Modal
        open={comparePickerOpen}
        width={520}
        footer={null}
        destroyOnHidden
        title={t('console.compareConfigTitle')}
        onCancel={() => setComparePickerOpen(false)}
      >
        {reportManifestError ? <Alert type="error" showIcon message={reportManifestError} /> : null}
        {consoleController.session?.id ? (
          <CompareConfigPanel
            sessionId={consoleController.session.id}
            candidates={compareCandidates}
            initial={defaultCompareSelection}
            onStart={(selection) => {
              setComparePickerOpen(false);
              setCompareConfig(selection);
              setCompareOpen(true);
            }}
          />
        ) : null}
      </Modal>
    </div>
  );
}
import { uiRegistry } from '@/features/console/ui-runtime/registry';
import { isArtifactReady, isArtifactFailed } from '@/features/console/ui-runtime/artifactAdapter';
