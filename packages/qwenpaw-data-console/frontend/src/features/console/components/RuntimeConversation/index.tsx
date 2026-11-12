import { useOnceVisible } from '../useOnceVisible';
import type { ModelConfigurationAdmission } from '../../model/chat/modelConfigurationGate';
import HistoryLoading from '../HistoryLoading';
import { useHistoryTopScroll } from '../useHistoryTopScroll';
import {
  mergeHydratedRuntimeMessages,
  shouldHydrateRuntimeHistory,
} from '../../model/runtime/runtimeSessionSync';
import RuntimeDataSelectors from './RuntimeDataSelectors';
import ResponseFailure from './ResponseFailure';
import { selectResponseFailure } from '../../model/runtime/responseFailure';
import AgentScopeRuntimeWebUI, {
  hasInputQueueItems,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI';
import type {
  IAgentScopeRuntimeWebUIOptions,
  IAgentScopeRuntimeWebUIInputData,
  IAgentScopeRuntimeWebUIQueueItemAction,
  IAgentScopeRuntimeWebUISessionAPI,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type { IAgentScopeRuntimeWebUIRef } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types/IRef';
import RuntimeMessage from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Message';
import RuntimeError from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Error';
import type { IAgentScopeRuntimeMessage } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import { AgentScopeRuntimeMessageType } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  getMessageElementInScrollContainer,
  getScrollVisibleTopOffset,
  scrollTargetIntoContainerTop,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Chat/MessageList/UserMessageAnchors/helpers';
import {
  BranchesOutlined,
  CompassOutlined,
  CopyOutlined,
  DeleteOutlined,
  DislikeOutlined,
  FileExcelOutlined,
  FileImageOutlined,
  FileOutlined,
  FilePdfOutlined,
  LikeOutlined,
  LoadingOutlined,
  NumberOutlined,
  SendOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Tooltip } from '@agentscope-ai/design';
import { useQwenPawDataColorMode } from '@qwenpaw-data/ui-tokens';
import { createRuntimeAntTheme, createRuntimeThemeOptions } from '@/theme';
import {
  ConfigProvider as AntConfigProvider,
  Input as AntInput,
  Modal,
  Popover,
  Radio,
  Upload,
  message,
} from 'antd';
import type { UploadProps } from 'antd';
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject } from 'react';
import {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { createPortal } from 'react-dom';
import { Coffee } from 'lucide-react';
import {
  ATTACHMENT_EXTENSIONS,
  attachmentExtension,
  createAttachmentReferenceUrl,
  parseAttachmentReferenceUrl,
  validateAttachmentFile,
} from '../../model/chat/attachments';
import {
  cacheRuntimeInputDraft,
  cacheRuntimeInputAttachments,
  consumeRuntimeInputAttachments,
  clearAcceptedRuntimeInputDraft,
  clearRuntimeInputDraft,
  getRuntimeInputAttachmentNames,
  getRuntimeInputDraft,
  getRuntimeInputAttachments,
  subscribeRuntimeInputAttachments,
  rememberRuntimeInputAttachmentName,
} from '../../model/chat/runtimeInputCache';
import type { ConsoleChatController } from '../../model/chat/useConsoleChat';
import { restoredAttachmentFiles, restoredAttachmentIds } from '../../model/chat/reviewSession';
import type { ArtifactComment, AttachmentRef } from '../../api/types';
import {
  parseRuntimeStreamChunk,
  resolveRequestAttachments,
  resolveRequestArtifactComments,
  selectRequestInputText,
  timelineItemToRuntimeMessage,
} from '../../model/runtime/runtimeMessages';
import {
  artifactCommentKey,
  artifactCommentRange,
  commentsFromMessageMetadata,
  toArtifactComment,
  visibleArtifactCommentText,
  type PendingArtifactComment,
} from '../../model/artifacts/artifactComments';
import {
  buildClarificationCancellationAnswers,
  claimClarificationSubmission,
  isAskUserQuestionExpired,
  selectPendingAskUserQuestion,
} from '../../model/chat/askUserQuestion';
import { serializeRuntimeResponseToMarkdown } from '../../model/artifacts/markdownContent';
import {
  extractLatestRuntimeAttachments,
  extractLatestRuntimeText,
  shouldClearAcceptedRuntimeAttachments,
  shouldClearAcceptedRuntimeInput,
} from '../../model/runtime/runtimeRequest';
import {
  isRuntimeQueueSteerVisible,
  runtimeQueueSteerOwnerOnly,
  shouldShowRuntimeStopButton,
  shouldUseRuntimeInputQueue,
} from '../../model/runtime/runtimeQueuePolicy';
import {
  isRuntimeToolInputMessage,
  isRuntimeToolMessage,
  isRuntimeToolOutputMessage,
  isRuntimeResponseRunning,
  toolMessageKey,
} from '../../model/runtime/runtimeProcess';
import { selectRuntimeProjectionPayload } from '../../model/runtime/runtimeProjection';
import {
  selectChatTimeline,
  splitChatTimelineBySteer,
  type ChatTimelineItem,
  type ChatTimelineSection,
} from '../../model/runtime/selectChatTimeline';
import {
  reconcileBackgroundRuntimeMessages,
  resolveRuntimeInputQueueSessionId,
  selectControlledRuntimeSessionId,
  selectRuntimeSessionMessages,
  selectRuntimeMessageSyncMode,
} from '../../model/runtime/runtimeSessionSync';
import {
  segmentInstanceKey,
  selectLastSessionSegmentKey,
} from '../../model/runtime/segmentExpansion';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import type { Artifact } from '../../types';
import { useAppI18n } from '@/i18n';
import RuntimeBizEventCard, {
  RuntimeToolBizEventCard,
  runtimeToolRenderConfig,
} from '../RuntimeBizEventCard';
import ExecutionDuration from '../ExecutionDuration';
import type { ExecutionTiming } from '../../api/types';
import RuntimeClarificationCard from '../RuntimeClarificationCard';
import clarificationStyles from '../RuntimeClarificationCard/index.module.less';
import RuntimeToolConfirmationCard from '../RuntimeToolConfirmationCard';
import {
  selectToolConfirmations,
  visibleToolConfirmations,
} from '../../model/chat/toolConfirmation';
import { confirmationKey } from '../../model/chat/toolConfirmationSubmission';
import RuntimeSegmentCard from '../RuntimeSegmentCard';
import RuntimeThinkingCard from '../RuntimeThinkingCard';
import ContextUsagePanel, { ContextUsageButton } from '../ContextUsage';
import { selectLatestContextUsage } from '../../model/runtime/contextUsage';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);
const EMPTY_SEGMENT_ID_SET: ReadonlySet<string> = new Set();
const EMPTY_PENDING_COMMENTS: PendingArtifactComment[] = [];

function ArtifactCommentChips({
  comments,
  onOpenComment,
}: {
  comments: readonly ArtifactComment[];
  onOpenComment?: (comment: ArtifactComment) => void;
}) {
  if (!comments.length) return null;
  return (
    <div className={styles.artifactCommentChips}>
      {comments.map((item) => (
        <button
          className={styles.artifactCommentChip}
          key={artifactCommentKey(item)}
          type="button"
          onClick={() => onOpenComment?.(item)}
        >
          <FileOutlined aria-hidden="true" />
          <span>{item.path}</span>
          <span>{artifactCommentRange(item)}</span>
        </button>
      ))}
    </div>
  );
}

function PendingArtifactComments({
  comments,
  rootRef,
  onClearComments,
  onOpenComment,
}: {
  comments: readonly PendingArtifactComment[];
  rootRef: RefObject<HTMLDivElement | null>;
  onClearComments?: () => void;
  onOpenComment?: (comment: ArtifactComment) => void;
}) {
  const { t } = useAppI18n();
  const [host, setHost] = useState<HTMLElement | null>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const discoverHost = () =>
      setHost(root.querySelector<HTMLElement>('.data-runtime-sender-content'));
    const observer = new MutationObserver(discoverHost);
    observer.observe(root, { childList: true, subtree: true });
    discoverHost();
    return () => observer.disconnect();
  }, [rootRef]);

  if (!comments.length || !host) return null;
  const popoverContent = (
    <div className={styles.pendingArtifactCommentPopoverContent}>
      <div className={styles.pendingArtifactCommentPopoverHeader}>
        <span>
          {t('artifact.commentAttached')}
          <strong>{comments.length}</strong>
        </span>
        <Button
          aria-label={t('artifact.commentClearAll')}
          className={styles.pendingArtifactCommentClearButton}
          danger
          icon={<DeleteOutlined />}
          size="small"
          title={t('artifact.commentClearAll')}
          type="text"
          onClick={onClearComments}
        />
      </div>
      <div className={styles.pendingArtifactCommentList}>
        {comments.map((item) => (
          <div className={styles.pendingArtifactCommentItem} key={artifactCommentKey(item)}>
            <button type="button" onClick={() => onOpenComment?.(item)}>
              <span className={styles.pendingArtifactCommentLocation}>
                <FileOutlined aria-hidden="true" />
                <span title={item.path}>{item.path}</span>
                <span>{artifactCommentRange(item)}</span>
              </span>
              <span className={styles.pendingArtifactCommentText}>{item.comment}</span>
            </button>
          </div>
        ))}
      </div>
    </div>
  );

  return createPortal(
    <div className={styles.pendingArtifactComments}>
      <Popover
        content={popoverContent}
        destroyOnHidden
        placement="topLeft"
        rootClassName={styles.pendingArtifactCommentPopover}
        trigger={['hover', 'focus']}
      >
        <button
          aria-label={`${t('artifact.commentAttached')} ${comments.length}`}
          className={styles.pendingArtifactCommentCapsule}
          type="button"
        >
          <FileOutlined aria-hidden="true" />
          <span>{t('artifact.commentAttached')}</span>
          <span className={styles.pendingArtifactCommentCount}>{comments.length}</span>
        </button>
      </Popover>
    </div>,
    host,
  );
}

const artifactCommentsBizParamKey = 'artifact_comments_json';

function artifactCommentsBizParams(comments: readonly PendingArtifactComment[]) {
  return {
    user_prompt_params: {
      [artifactCommentsBizParamKey]: JSON.stringify(comments.map(toArtifactComment)),
    },
  };
}

function artifactCommentsFromBizParams(
  bizParams: { user_prompt_params?: Record<string, string> } | undefined,
): PendingArtifactComment[] {
  const raw = bizParams?.user_prompt_params?.[artifactCommentsBizParamKey];
  if (!raw) return [];
  try {
    return commentsFromMessageMetadata({ artifact_comments: JSON.parse(raw) });
  } catch {
    return [];
  }
}

function requestAttachmentIcon(filename: string) {
  const ext = attachmentExtension(filename);
  if (ext === 'pdf') return <FilePdfOutlined />;
  if (ext === 'xlsx' || ext === 'xls' || ext === 'csv') return <FileExcelOutlined />;
  if (ext === 'png' || ext === 'jpg' || ext === 'jpeg') return <FileImageOutlined />;
  return <FileOutlined />;
}

function RuntimeRequestAttachments({ attachments }: { attachments: readonly AttachmentRef[] }) {
  if (!attachments.length) return null;
  return (
    <ul className={styles.runtimeRequestAttachmentList}>
      {attachments.map((item) => (
        <li
          className={styles.runtimeRequestAttachmentCard}
          data-ext={attachmentExtension(item.filename) || 'file'}
          key={item.attachment_id}
        >
          <span className={styles.runtimeRequestAttachmentIcon} aria-hidden="true">
            {requestAttachmentIcon(item.filename)}
          </span>
          <span className={styles.runtimeRequestAttachmentMeta}>
            <span className={styles.runtimeRequestAttachmentName} title={item.filename}>
              {item.filename}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

interface RuntimeConversationProps {
  controller: ConsoleChatController;
  pendingComments?: PendingArtifactComment[];
  composerNotice?: ReactNode;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
  onCreateSkillDraft?: (chatId: string) => Promise<void>;
  onOpenAgentConfiguration?: () => void;
  onOpenDataBridgeModelConfig?: () => void;
  onOpenComment?: (comment: ArtifactComment) => void;
  onConsumePendingComments?: () => void;
  onRestorePendingComments?: (comments: readonly PendingArtifactComment[]) => void;
}

export interface RuntimeConversationHandle {
  scrollToChat: (chatId: string) => void;
}

const runtimeScrollContainerSelector = '.data-runtime-chat-anywhere-message-list-bubble-scroll';
const runtimeInputSelector = '.data-runtime-chat-anywhere-input';
const runtimeInputHeightProperty = '--runtime-input-height';
const runtimeAnchorActiveClass = 'data-runtime-chat-anywhere-message-list-anchor-target-active';
const runtimeAnchorActiveDuration = 1200;
const dislikeReasons = [
  { value: '分析不准确', labelKey: 'runtime.reasonInaccurate' },
  { value: '没有解决问题', labelKey: 'runtime.reasonUnresolved' },
  { value: '内容不完整', labelKey: 'runtime.reasonIncomplete' },
  { value: '表达不清晰', labelKey: 'runtime.reasonUnclear' },
  { value: '其他', labelKey: 'runtime.reasonOther' },
] as const;

async function writeClipboardText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through for local HTTP and embedded browsers without clipboard permission.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = 'fixed';
  textarea.style.inset = '0 auto auto -9999px';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('Clipboard write failed');
}

function runtimeInputTextarea(root: HTMLElement | null): HTMLTextAreaElement | null {
  return root?.querySelector<HTMLTextAreaElement>('textarea.data-runtime-sender-input') ?? null;
}

function setRuntimeInputValue(textarea: HTMLTextAreaElement, value: string) {
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
  valueSetter?.call(textarea, value);
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
}

function writeRuntimeInput(
  root: HTMLElement | null,
  text: string,
  notReadyMessage: string,
): boolean {
  const textarea = runtimeInputTextarea(root);
  if (!textarea) {
    message.warning(notReadyMessage);
    return false;
  }
  setRuntimeInputValue(textarea, text);
  textarea.focus();
  textarea.setSelectionRange(text.length, text.length);
  return true;
}

function fillRuntimeInput(
  root: HTMLElement | null,
  text: string,
  notReadyMessage: string,
): boolean {
  return writeRuntimeInput(root, text, notReadyMessage);
}

function clearRuntimeInputAfterAccepted(
  root: HTMLElement | null,
  submittedText: string,
  submittedAttachmentCount = 0,
) {
  const textarea = runtimeInputTextarea(root);
  if (textarea && shouldClearAcceptedRuntimeInput(textarea.value, submittedText)) {
    setRuntimeInputValue(textarea, '');
  }

  const attachmentRemoveButtons = root
    ? Array.from(
        root.querySelectorAll<HTMLButtonElement>('[class$="-attachment-list-card-remove"]'),
      )
    : [];
  if (
    !shouldClearAcceptedRuntimeAttachments(attachmentRemoveButtons.length, submittedAttachmentCount)
  )
    return;

  attachmentRemoveButtons.forEach((button) => button.click());
}

type RuntimeDisplayEntry = {
  kind: 'message';
  message: IAgentScopeRuntimeMessage;
};

type RuntimeResponseSection =
  | ChatTimelineSection
  | {
      kind: 'response';
      id: string;
      items: null;
    };

function mergeRuntimeDisplayEntries(entries: RuntimeDisplayEntry[]): RuntimeDisplayEntry[] {
  const result: RuntimeDisplayEntry[] = [];
  const pendingToolIndexes = new Map<string, number>();

  entries.forEach((entry) => {
    const { message: runtimeMessage } = entry;
    if (runtimeMessage.type === AgentScopeRuntimeMessageType.HEARTBEAT) return;
    // A message-start event can arrive before its first text delta. Do not
    // reserve a timeline row until it contains something visible.
    if (
      runtimeMessage.type === AgentScopeRuntimeMessageType.MESSAGE &&
      !selectRuntimeProjectionPayload(runtimeMessage) &&
      runtimeMessage.content.every((content) => content.type === 'text' && !content.text?.trim())
    )
      return;

    if (isRuntimeToolInputMessage(runtimeMessage)) {
      pendingToolIndexes.set(toolMessageKey(runtimeMessage), result.length);
      result.push(entry);
      return;
    }

    if (!isRuntimeToolOutputMessage(runtimeMessage)) {
      result.push(entry);
      return;
    }

    const key = toolMessageKey(runtimeMessage);
    const inputIndex = pendingToolIndexes.get(key);
    if (inputIndex === undefined) {
      result.push(entry);
      return;
    }

    const inputEntry = result[inputIndex];
    if (inputEntry?.kind !== 'message') {
      result.push(entry);
      return;
    }
    result[inputIndex] = {
      kind: 'message',
      message: {
        ...runtimeMessage,
        content: [...inputEntry.message.content, ...runtimeMessage.content],
      },
    };
    pendingToolIndexes.delete(key);
  });

  return result;
}

const RuntimeProcessTimeline = memo(function RuntimeProcessTimeline({
  messages,
  timeline,
  showConfidence,
  terminal,
  chatId,
  sessionId,
  initialHistoricalSegmentKeys,
  lastSessionSegmentKey,
  onArtifactOpen,
  onArtifactDownload,
  onArtifactShare,
}: {
  messages: IAgentScopeRuntimeMessage[];
  timeline?: ChatTimelineItem[] | null;
  showConfidence: boolean;
  terminal: boolean;
  chatId?: string | null;
  sessionId?: string | null;
  initialHistoricalSegmentKeys: ReadonlySet<string>;
  lastSessionSegmentKey: string | null;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
}) {
  const { t } = useAppI18n();
  const timelineEntries = timeline
    ? timeline.flatMap<RuntimeDisplayEntry>((item) => {
        if (item.kind === 'user') {
          return [];
        }
        // The initial user request is already rendered by the SDK request card.
        // A user-shaped BizEvent is its business projection and must not duplicate it
        // inside the assistant response timeline.
        if (item.kind === 'biz_event' && item.event.presentation?.card_type === 'user') {
          return [];
        }
        return [
          {
            kind: 'message',
            message: timelineItemToRuntimeMessage(item, terminal),
          },
        ];
      })
    : messages.map<RuntimeDisplayEntry>((runtimeMessage) => ({
        kind: 'message',
        message: runtimeMessage,
      }));
  const displayEntries = mergeRuntimeDisplayEntries(timelineEntries);
  if (!displayEntries.length) return null;
  const segmentIndexes = new Map<string, number>();
  displayEntries.forEach((entry) => {
    const projection = selectRuntimeProjectionPayload(entry.message);
    if (projection?.kind === 'segment') {
      segmentIndexes.set(entry.message.id, segmentIndexes.size + 1);
    }
  });

  return (
    <div className={styles.runtimeProcessTimeline} aria-label={t('runtime.process')}>
      {displayEntries.map((entry) => {
        const { message: runtimeMessage } = entry;
        const projection = selectRuntimeProjectionPayload(runtimeMessage);
        if (projection?.kind === 'segment') {
          return (
            <RuntimeSegmentCard
              data={projection.segment}
              showConfidence={showConfidence}
              events={projection.events}
              showTerminalEvent={Boolean(
                chatId &&
                lastSessionSegmentKey === segmentInstanceKey(chatId, projection.segment.segment_id),
              )}
              defaultOpen={
                !chatId ||
                !initialHistoricalSegmentKeys.has(
                  segmentInstanceKey(chatId, projection.segment.segment_id),
                )
              }
              sessionId={sessionId}
              chatId={chatId}
              index={segmentIndexes.get(runtimeMessage.id) ?? 1}
              key={runtimeMessage.id}
              onArtifactOpen={onArtifactOpen}
              onArtifactDownload={onArtifactDownload}
              onArtifactShare={onArtifactShare}
            />
          );
        }
        if (projection?.kind === 'biz_event') {
          return <RuntimeBizEventCard data={projection.event} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.REASONING) {
          return (
            <div key={runtimeMessage.id}>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <ExecutionDuration
                  timing={
                    (runtimeMessage as typeof runtimeMessage & { timing?: ExecutionTiming }).timing
                  }
                />
              </div>
              <RuntimeThinkingCard data={runtimeMessage} />
            </div>
          );
        }
        if (isRuntimeToolMessage(runtimeMessage)) {
          return <RuntimeToolBizEventCard data={runtimeMessage} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.ERROR) {
          return <RuntimeError data={runtimeMessage} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.MESSAGE) {
          return (
            <div key={runtimeMessage.id}>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <ExecutionDuration
                  timing={
                    (runtimeMessage as typeof runtimeMessage & { timing?: ExecutionTiming }).timing
                  }
                />
              </div>
              <RuntimeMessage data={runtimeMessage} />
            </div>
          );
        }
        return null;
      })}
    </div>
  );
});

function RuntimeSteerMessage({
  comments,
  onOpenComment,
  text,
}: {
  comments: ArtifactComment[];
  onOpenComment?: (comment: ArtifactComment) => void;
  text: string;
}) {
  const { t } = useAppI18n();
  const visibleText = comments.length ? visibleArtifactCommentText(text) : text;
  return (
    <article className={styles.runtimeSteerRequestShell} aria-label={t('runtime.steerMessage')}>
      <ArtifactCommentChips comments={comments} onOpenComment={onOpenComment} />
      {visibleText ? (
        <div className={styles.runtimeSteerRequestBubble}>
          <span className={styles.runtimeSteerRequestIcon} aria-hidden="true">
            <CompassOutlined />
          </span>
          <span>{visibleText}</span>
        </div>
      ) : null}
    </article>
  );
}

function RuntimeResponseActions({
  readOnly,
  chatId,
  chatStatus,
  text,
  forkable,
  forkDisabled,
  onFeedback,
  onFork,
  loadReaction,
  clearReaction,
  onCreateSkillDraft,
}: {
  readOnly: boolean;
  chatId: string;
  chatStatus?: string;
  text: string;
  forkable: boolean;
  forkDisabled: boolean;
  onFeedback: (
    kind: 'like' | 'dislike' | 'copy',
    detail?: { reason?: string | null; detail?: string | null },
  ) => Promise<void>;
  onFork: (chatId: string) => Promise<void>;
  loadReaction: (chatId: string) => Promise<'like' | 'dislike' | null>;
  clearReaction: (chatId: string) => Promise<void>;
  onCreateSkillDraft?: (chatId: string) => Promise<void>;
}) {
  const { t } = useAppI18n();
  const { ref: actionsRef, visible: actionsVisible } = useOnceVisible<HTMLElement>();
  const [reaction, setReaction] = useState<'like' | 'dislike' | null>(null);
  const [reactionLoading, setReactionLoading] = useState(true);
  const [submittingReaction, setSubmittingReaction] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [requestIdCopyState, setRequestIdCopyState] = useState<'idle' | 'copied' | 'failed'>(
    'idle',
  );
  const [dislikeOpen, setDislikeOpen] = useState(false);
  const [dislikeReason, setDislikeReason] = useState('');
  const [dislikeDetail, setDislikeDetail] = useState('');
  const [submittingDislike, setSubmittingDislike] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState(false);
  const [forking, setForking] = useState(false);
  const canCreateSkillDraft = Boolean(
    onCreateSkillDraft && chatStatus && ['completed', 'failed', 'canceled'].includes(chatStatus),
  );

  useEffect(() => {
    let active = true;
    setReactionLoading(true);
    if (!actionsVisible) return;
    void loadReaction(chatId)
      .then((value) => {
        if (active) setReaction(value);
      })
      .catch(() => {
        if (active) setReaction(null);
      })
      .finally(() => {
        if (active) setReactionLoading(false);
      });
    return () => {
      active = false;
    };
  }, [chatId, loadReaction, actionsVisible]);

  const handleCopy = async () => {
    if (!text) return;
    try {
      await writeClipboardText(text);
      setCopyState('copied');
      if (!readOnly) void onFeedback('copy').catch(() => undefined);
    } catch {
      setCopyState('failed');
    }
    window.setTimeout(() => setCopyState('idle'), 1200);
  };

  const handleFork = async () => {
    setForking(true);
    try {
      await onFork(chatId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('runtime.createBranchFailed'));
      setForking(false);
    }
  };

  const handleCopyRequestId = async () => {
    try {
      await writeClipboardText(chatId);
      setRequestIdCopyState('copied');
    } catch {
      setRequestIdCopyState('failed');
    }
    window.setTimeout(() => setRequestIdCopyState('idle'), 1200);
  };

  const handleLike = async () => {
    setSubmittingReaction(true);
    try {
      if (reaction === 'like') {
        await clearReaction(chatId);
        setReaction(null);
      } else {
        await onFeedback('like');
        setReaction('like');
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('runtime.likeFailed'));
    } finally {
      setSubmittingReaction(false);
    }
  };

  const clearDislike = async () => {
    setSubmittingReaction(true);
    try {
      await clearReaction(chatId);
      setReaction(null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('runtime.clearDislikeFailed'));
    } finally {
      setSubmittingReaction(false);
    }
  };

  const submitDislike = async () => {
    if (!dislikeReason) {
      message.warning(t('runtime.selectFeedbackReason'));
      return;
    }
    setSubmittingDislike(true);
    try {
      await onFeedback('dislike', {
        reason: dislikeReason,
        detail: dislikeDetail.trim() || null,
      });
      setReaction('dislike');
      setDislikeOpen(false);
      setDislikeReason('');
      setDislikeDetail('');
      message.success(t('runtime.feedbackThanks'));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('runtime.feedbackFailed'));
    } finally {
      setSubmittingDislike(false);
    }
  };

  return (
    <footer
      ref={actionsRef}
      className={styles.runtimeResponseActions}
      aria-label={t('runtime.responseActions')}
    >
      <Tooltip
        title={
          copyState === 'copied'
            ? t('runtime.copiedMarkdown')
            : copyState === 'failed'
              ? t('runtime.copyFailed')
              : t('runtime.copyMarkdown')
        }
        trigger={['hover', 'focus']}
      >
        <Button
          type="text"
          icon={<CopyOutlined />}
          aria-label={
            copyState === 'copied' ? t('runtime.copiedMarkdown') : t('runtime.copyResponse')
          }
          disabled={!text}
          onClick={handleCopy}
        />
      </Tooltip>
      {!readOnly ? (
        <>
          <Tooltip title={t('runtime.helpful')} trigger={['hover', 'focus']}>
            <Button
              className={reaction === 'like' ? styles.runtimeResponseActionActive : ''}
              type="text"
              icon={<LikeOutlined />}
              aria-label={t('runtime.like')}
              disabled={reactionLoading || submittingReaction || submittingDislike}
              onClick={() => void handleLike()}
            />
          </Tooltip>
          <Tooltip title={t('runtime.needsImprovement')} trigger={['hover', 'focus']}>
            <Button
              className={reaction === 'dislike' ? styles.runtimeResponseActionActive : ''}
              type="text"
              icon={<DislikeOutlined />}
              aria-label={t('runtime.dislike')}
              disabled={reactionLoading || submittingReaction || submittingDislike}
              onClick={() => {
                if (reaction === 'dislike') {
                  void clearDislike();
                } else {
                  setDislikeOpen(true);
                }
              }}
            />
          </Tooltip>
        </>
      ) : null}
      <Tooltip
        title={
          requestIdCopyState === 'copied'
            ? t('runtime.copiedRequestId')
            : requestIdCopyState === 'failed'
              ? t('runtime.copyFailed')
              : t('runtime.copyRequestId')
        }
        trigger={['hover', 'focus']}
      >
        <Button
          type="text"
          icon={<NumberOutlined />}
          aria-label={t('runtime.copyRequestId')}
          onClick={() => void handleCopyRequestId()}
        />
      </Tooltip>
      {!readOnly ? (
        <Tooltip
          title={canCreateSkillDraft ? t('skillDraft.create') : t('skillDraft.createHint')}
          trigger={['hover', 'focus']}
        >
          <Button
            type="text"
            icon={creatingDraft ? <LoadingOutlined /> : <ThunderboltOutlined />}
            aria-label={t('skillDraft.create')}
            disabled={!canCreateSkillDraft || creatingDraft}
            onClick={() => {
              if (!canCreateSkillDraft || !onCreateSkillDraft) return;
              setCreatingDraft(true);
              void onCreateSkillDraft(chatId).finally(() => setCreatingDraft(false));
            }}
          />
        </Tooltip>
      ) : null}
      {forkable && !readOnly ? (
        <Tooltip
          title={forking ? t('runtime.creatingBranch') : t('runtime.createBranch')}
          trigger={['hover', 'focus']}
        >
          <Button
            type="text"
            icon={<BranchesOutlined />}
            aria-label={t('runtime.createBranch')}
            disabled={forking || forkDisabled}
            onClick={() => void handleFork()}
          />
        </Tooltip>
      ) : null}
      <Modal
        open={dislikeOpen && !readOnly}
        title={t('runtime.feedbackTitle')}
        okText={t('runtime.submitFeedback')}
        cancelText={t('common.cancel')}
        confirmLoading={submittingDislike}
        onCancel={() => setDislikeOpen(false)}
        onOk={() => void submitDislike()}
      >
        <div className={styles.dislikeForm}>
          <Radio.Group
            value={dislikeReason}
            onChange={(event) => setDislikeReason(event.target.value)}
          >
            {dislikeReasons.map((reason) => (
              <Radio value={reason.value} key={reason.value}>
                {t(reason.labelKey)}
              </Radio>
            ))}
          </Radio.Group>
          <AntInput.TextArea
            rows={4}
            maxLength={500}
            showCount
            value={dislikeDetail}
            placeholder={t('runtime.feedbackDetail')}
            onChange={(event) => setDislikeDetail(event.target.value)}
          />
        </div>
      </Modal>
    </footer>
  );
}

function RuntimeFollowupHints({
  questions,
  onSelect,
}: {
  questions: string[];
  onSelect: (question: string) => void;
}) {
  const { t } = useAppI18n();
  if (!questions.length) return null;
  return (
    <div className={styles.followupHints} aria-label={t('runtime.followupsLabel')}>
      <span>{t('runtime.followups')}</span>
      {questions.map((question) => (
        <button type="button" key={question} onClick={() => onSelect(question)}>
          {question}
        </button>
      ))}
    </div>
  );
}

function RuntimeResponseMessage({
  messages,
  timeline,
  showConfidence,
  terminal,
  chatId,
  sessionId,
  ensureChatTrace,
  initialHistoricalSegmentKeys,
  lastSessionSegmentKey,
  running,
  waitingForInput,
  beforeSteer,
  afterSteer,
  footer,
  failure,
  readOnly,
  onArtifactOpen,
  onArtifactDownload,
  onArtifactShare,
}: {
  messages: IAgentScopeRuntimeMessage[];
  timeline?: ChatTimelineItem[] | null;
  showConfidence: boolean;
  terminal: boolean;
  chatId?: string | null;
  sessionId?: string | null;
  ensureChatTrace: (chatId: string) => void;
  initialHistoricalSegmentKeys: ReadonlySet<string>;
  lastSessionSegmentKey: string | null;
  running: boolean;
  waitingForInput?: boolean;
  beforeSteer?: boolean;
  afterSteer?: boolean;
  footer?: ReactNode;
  failure?: ReturnType<typeof selectResponseFailure>;
  readOnly: boolean;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
}) {
  const { t } = useAppI18n();
  const { ref, visible } = useOnceVisible<HTMLElement>();
  useEffect(() => {
    if (visible && chatId) ensureChatTrace(chatId);
  }, [visible, chatId, ensureChatTrace]);
  return (
    <article
      ref={ref}
      className={`${styles.runtimeResponseRow} ${beforeSteer ? styles.runtimeResponseRowBeforeSteer : ''} ${afterSteer ? styles.runtimeResponseRowAfterSteer : ''}`}
    >
      <div
        className={`${styles.runtimeMessageAvatar} ${styles.runtimeAgentAvatar}`}
        aria-hidden="true"
      >
        <img src={`${import.meta.env.BASE_URL}mascot-blue-transparent.png`} alt="" />
      </div>
      <div className={styles.runtimeResponseColumn}>
        <div className={styles.runtimeResponseSurface}>
          <div className={styles.runtimeResponseContent}>
            <RuntimeProcessTimeline
              messages={messages}
              timeline={timeline}
              showConfidence={showConfidence}
              terminal={terminal}
              chatId={chatId}
              sessionId={sessionId}
              initialHistoricalSegmentKeys={initialHistoricalSegmentKeys}
              lastSessionSegmentKey={lastSessionSegmentKey}
              onArtifactOpen={onArtifactOpen}
              onArtifactDownload={onArtifactDownload}
              onArtifactShare={onArtifactShare}
            />
          </div>
          {failure ? <ResponseFailure failure={failure} readOnly={readOnly} /> : null}
          {running ? (
            <div className={styles.runtimeResponseLoading} role="status" aria-live="polite">
              <LoadingOutlined spin aria-hidden="true" />
              <span>
                {waitingForInput ? t('runtime.waitingConfirmation') : t('runtime.generating')}
              </span>
            </div>
          ) : null}
        </div>
        {footer}
      </div>
    </article>
  );
}

const RuntimeConversation = forwardRef<RuntimeConversationHandle, RuntimeConversationProps>(
  function RuntimeConversation(
    {
      controller,
      pendingComments = EMPTY_PENDING_COMMENTS,
      composerNotice,
      onArtifactOpen,
      onArtifactDownload,
      onArtifactShare,
      onCreateSkillDraft,
      onOpenAgentConfiguration,
      onOpenDataBridgeModelConfig,
      onOpenComment,
      onConsumePendingComments,
      onRestorePendingComments,
    },
    forwardedRef,
  ) {
    const { language, t } = useAppI18n();
    const { colorMode } = useQwenPawDataColorMode();
    const lastSessionSegmentKey = useMemo(
      () => selectLastSessionSegmentKey(controller.projections.map(({ chat }) => chat)),
      [controller.projections],
    );
    const runtimeAntTheme = useMemo(() => createRuntimeAntTheme(colorMode), [colorMode]);
    const rootRef = useRef<HTMLDivElement>(null);
    const manualScrollUntilRef = useRef(0);
    useHistoryTopScroll(
      rootRef,
      controller.hasMoreHistory && !controller.historyLoading && !controller.historyError,
      controller.loadOlderChats,
      runtimeScrollContainerSelector,
    );
    const runtimeRef = useRef<IAgentScopeRuntimeWebUIRef>(null);
    const runtimeInputCacheKey = controller.session?.id ?? controller.inputQueueSessionId;
    const [contextUsageOpenFor, setContextUsageOpenFor] = useState<string | null>(null);
    const contextUsageOpen = contextUsageOpenFor === runtimeInputCacheKey;
    const contextUsage = selectLatestContextUsage(controller.projections, controller.session?.id);
    const runtimeInputCacheKeyRef = useRef(runtimeInputCacheKey);
    const pendingAttachments = useSyncExternalStore(subscribeRuntimeInputAttachments, () =>
      getRuntimeInputAttachments(runtimeInputCacheKey),
    );
    const lastPendingSubmissionRef = useRef<{
      sessionId: string;
      attachments: readonly AttachmentRef[];
      draft: string;
    } | null>(null);
    const pendingSubmitRef = useRef<(text: string) => void>(() => {});
    const coffeeModeEnabled = !controller.enableClarification;
    const pendingCommentsRef = useRef(pendingComments);
    pendingCommentsRef.current = pendingComments;
    const syncedRuntimeSessionIdRef = useRef<string | null>(null);
    const runtimeSessionGeneratingRef = useRef(false);
    const runtimeInputDraftRef = useRef(getRuntimeInputDraft(runtimeInputCacheKey));
    const runtimeInputLifecycleRef = useRef({
      connectionStatus: controller.connectionStatus,
      sessionId: controller.runtimeSession?.id,
    });
    const runtimeInputRestoreFrameRef = useRef<number | null>(null);
    const backgroundQueueMessageIdsRef = useRef(new Set<string>());
    const scrollSequenceRef = useRef(0);
    const cancelChatJumpRef = useRef<(() => void) | null>(null);
    const anchorHighlightTimerRef = useRef<number | null>(null);
    const attachmentUploadsInFlightRef = useRef(new Set<Blob>());
    const cmAdmissionPendingRef = useRef(false);
    const modelAdmissionRef = useRef<ModelConfigurationAdmission | undefined>();
    const [steeringItemId, setSteeringItemId] = useState<string | null>(null);
    const [dismissedClarificationKeys, setDismissedClarificationKeys] = useState(
      () => new Set<string>(),
    );
    const claimedClarificationKeysRef = useRef(new Set<string>());
    const runtimeSession = controller.runtimeSession;
    const currentChatId = controller.currentChatId;
    const currentChatStatus = controller.currentProjection?.chat.status;
    const answerClarification = controller.answerClarification;
    const confirmationState = useSyncExternalStore(
      controller.toolConfirmationSubmissions.subscribe,
      controller.toolConfirmationSubmissions.getSnapshot,
    );
    const toolConfirmations = useMemo(
      () =>
        controller.currentProjection
          ? selectToolConfirmations(
              controller.currentProjection.messages,
              controller.currentProjection.chat.id,
            )
          : [],
      [controller.currentProjection],
    );
    const [, refreshConfirmationClock] = useState(0);
    const visibleConfirmations = visibleToolConfirmations(toolConfirmations, {
      status: currentChatStatus,
      stopping: controller.toolConfirmationStopping,
    });
    const hasToolConfirmations = visibleConfirmations.length > 0;
    useEffect(() => {
      if (!hasToolConfirmations) return;
      const timer = window.setInterval(() => refreshConfirmationClock((tick) => tick + 1), 1000);
      return () => window.clearInterval(timer);
    }, [hasToolConfirmations]);
    const steerCurrentChat = controller.steerCurrentChat;
    const [controlledRuntimeSessionId, setControlledRuntimeSessionId] = useState(
      runtimeSession?.id,
    );
    const runtimeInputMountKey = [
      controller.inputQueueSessionId,
      controlledRuntimeSessionId ?? 'new',
      controller.runtimeSnapshotVersion,
      controller.accessVersion,
      controller.readOnly ? 'read' : 'write',
    ].join(':');
    const liveUploads = useMemo(
      () => ({
        mountKey: runtimeInputMountKey,
        sessionId: runtimeInputCacheKey,
        attachments: [] as AttachmentRef[],
      }),
      [runtimeInputCacheKey, runtimeInputMountKey],
    );
    useEffect(
      () => () => {
        cacheRuntimeInputAttachments(liveUploads.sessionId, liveUploads.attachments);
      },
      [liveUploads],
    );
    // SDK queue callbacks can outlive their mounted session. Never let an old
    // callback resolve the shared runtimeRef to the newly mounted conversation.
    const runtimeScope = useMemo(
      () => ({ mountKey: runtimeInputMountKey, sessionId: runtimeSession?.id }),
      [runtimeInputMountKey, runtimeSession?.id],
    );
    const activeRuntimeScopeRef = useRef(runtimeScope);
    activeRuntimeScopeRef.current = runtimeScope;
    const isCurrentRuntime = useCallback(
      () => activeRuntimeScopeRef.current === runtimeScope,
      [runtimeScope],
    );
    const clarificationCandidate =
      !controller.readOnly &&
      controller.currentProjection?.chat.status === 'running' &&
      controller.currentProjection.chat.enable_clarification !== false
        ? selectPendingAskUserQuestion(controller.currentProjection.messages)
        : null;
    const clarificationKey =
      clarificationCandidate && currentChatId
        ? `${currentChatId}:${clarificationCandidate.id}`
        : null;
    const clarificationCandidateId = clarificationCandidate?.id ?? null;
    const clarificationExpiresAt = clarificationCandidate?.expires_at ?? null;
    const clarificationDismissed = clarificationKey
      ? dismissedClarificationKeys.has(clarificationKey)
      : false;
    const clarificationExpired = clarificationCandidate
      ? isAskUserQuestionExpired(clarificationCandidate)
      : false;
    const pendingClarification =
      clarificationCandidate && !clarificationDismissed && !clarificationExpired
        ? clarificationCandidate
        : null;

    const beforeAttachmentUpload = useCallback<NonNullable<UploadProps['beforeUpload']>>(
      (file) => {
        const cacheKey = runtimeInputCacheKeyRef.current;
        const reason = validateAttachmentFile(file, getRuntimeInputAttachmentNames(cacheKey));
        if (!reason) {
          rememberRuntimeInputAttachmentName(cacheKey, file.name);
          return true;
        }
        if (reason === 'empty') message.warning(t('runtime.attachmentEmpty'));
        if (reason === 'tooLarge') message.warning(t('runtime.attachmentTooLarge'));
        if (reason === 'type') message.warning(t('runtime.attachmentType'));
        if (reason === 'duplicate') message.warning(t('runtime.attachmentDuplicate'));
        return Upload.LIST_IGNORE;
      },
      [t],
    );

    const uploadAttachment = useCallback<NonNullable<UploadProps['customRequest']>>(
      async (options) => {
        if (!(options.file instanceof File)) {
          const error = new Error(t('runtime.attachmentUploadFailed'));
          options.onError?.(error);
          message.error(error.message);
          return;
        }

        attachmentUploadsInFlightRef.current.add(options.file);
        try {
          const attachment = await controller.uploadAttachment(options.file);
          liveUploads.attachments = [...liveUploads.attachments, attachment];
          options.onProgress?.({ percent: 100 });
          options.onSuccess?.({
            ...attachment,
            url: createAttachmentReferenceUrl(attachment),
          });
        } catch (error) {
          const uploadError =
            error instanceof Error ? error : new Error(t('runtime.attachmentUploadFailed'));
          options.onError?.(uploadError);
          message.error(uploadError.message);
        } finally {
          attachmentUploadsInFlightRef.current.delete(options.file);
        }
      },
      [controller, liveUploads, t],
    );

    const restorePendingSubmission = useCallback(() => {
      const submitted = lastPendingSubmissionRef.current;
      if (!submitted) return;
      cacheRuntimeInputAttachments(submitted.sessionId, submitted.attachments);
      if (!getRuntimeInputDraft(submitted.sessionId)) {
        cacheRuntimeInputDraft(submitted.sessionId, submitted.draft);
      }
      if (runtimeInputCacheKeyRef.current === submitted.sessionId) {
        const textarea = runtimeInputTextarea(rootRef.current);
        if (textarea && !textarea.value) setRuntimeInputValue(textarea, submitted.draft);
      }
      lastPendingSubmissionRef.current = null;
    }, []);

    const preparePendingAttachments = useCallback(
      (data: IAgentScopeRuntimeWebUIInputData) => {
        const sessionId = runtimeInputCacheKey;
        const attachments = [
          ...new Map(
            [...liveUploads.attachments, ...getRuntimeInputAttachments(sessionId)].map((item) => [
              item.attachment_id,
              item,
            ]),
          ).values(),
        ];
        lastPendingSubmissionRef.current = { sessionId, attachments, draft: data.query };
        if (attachments.length) {
          const files = restoredAttachmentFiles(attachments);
          const ids = new Set(files.map((file) => file.uid));
          data.fileList = [...(data.fileList ?? []).filter((file) => !ids.has(file.uid)), ...files];
          data.attachments = [...data.fileList];
          data.biz_params = {
            ...data.biz_params,
            user_prompt_params: {
              ...data.biz_params?.user_prompt_params,
              qwenpaw_data_pending_attachment_ids: JSON.stringify(
                attachments.map((item) => item.attachment_id),
              ),
            },
          };
          consumeRuntimeInputAttachments(
            sessionId,
            attachments.map((item) => item.attachment_id),
          );
        }
        // Submitted SDK files now belong to the request/queue, not the draft restored on navigation.
        liveUploads.attachments = [];
      },
      [liveUploads, runtimeInputCacheKey],
    );

    const canAdmitRuntimeInput = useCallback(async () => {
      // Repeated Enter/clicks while probing must not submit the same draft twice.
      if (cmAdmissionPendingRef.current) return false;
      cmAdmissionPendingRef.current = true;
      const scope = runtimeInputCacheKeyRef.current;
      try {
        const assertAccess = controller.captureWriteAccess();
        modelAdmissionRef.current = undefined;
        modelAdmissionRef.current = await controller.ensureModelConfiguration();
        assertAccess();
        return (
          Boolean(rootRef.current) &&
          scope === runtimeInputCacheKeyRef.current &&
          isCurrentRuntime()
        );
      } catch {
        return false;
      } finally {
        cmAdmissionPendingRef.current = false;
      }
    }, [controller, isCurrentRuntime]);

    const beforeRuntimeSubmit = useCallback(
      async (_data: IAgentScopeRuntimeWebUIInputData) => {
        if (attachmentUploadsInFlightRef.current.size > 0) {
          message.warning(t('runtime.attachmentUploading'));
          return false;
        }
        if (getRuntimeInputAttachments(runtimeInputCacheKeyRef.current).length) {
          pendingSubmitRef.current(runtimeInputTextarea(rootRef.current)?.value ?? '');
          return false;
        }
        if (!(await canAdmitRuntimeInput())) return false;
        // A later ordinary submission must not roll back a report already handed to the SDK queue.
        lastPendingSubmissionRef.current = null;
        liveUploads.attachments = [];
        if (currentChatStatus === 'running') {
          // A running Chat submits into the queue. Do not later restore text that
          // the user intentionally queued when the current SSE completes.
          runtimeInputDraftRef.current = '';
          clearRuntimeInputDraft(runtimeInputCacheKeyRef.current);
        }
        window.setTimeout(() => runtimeInputTextarea(rootRef.current)?.blur(), 0);
        return true;
      },
      [canAdmitRuntimeInput, currentChatStatus, liveUploads, t],
    );

    const resolveClarification = useCallback(
      async (
        clarificationId: string,
        result: Parameters<ConsoleChatController['answerClarification']>[2],
      ) => {
        const chatId = currentChatId;
        if (!chatId) throw new Error(t('runtime.chatNotLoaded'));
        const key = `${chatId}:${clarificationId}`;
        if (!claimClarificationSubmission(claimedClarificationKeysRef.current, key)) return;
        setDismissedClarificationKeys((current) => {
          if (current.has(key)) return current;
          const next = new Set(current);
          next.add(key);
          return next;
        });
        await answerClarification(chatId, clarificationId, result);
      },
      [answerClarification, currentChatId, t],
    );

    useEffect(() => {
      if (
        !clarificationCandidateId ||
        !clarificationKey ||
        clarificationDismissed ||
        !clarificationExpiresAt
      ) {
        return undefined;
      }
      const deadline = Date.parse(clarificationExpiresAt);
      if (Number.isNaN(deadline)) return undefined;
      const submitTimeout = () => {
        void resolveClarification(clarificationCandidateId, {
          status: 'timeout',
          reason: t('runtime.timeout'),
        }).catch(() => undefined);
      };
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        submitTimeout();
        return undefined;
      }
      const timer = window.setTimeout(submitTimeout, remaining);
      return () => window.clearTimeout(timer);
    }, [
      clarificationCandidateId,
      clarificationDismissed,
      clarificationExpiresAt,
      clarificationKey,
      resolveClarification,
      t,
    ]);

    const scrollToChat = useCallback((chatId: string) => {
      const sequence = scrollSequenceRef.current + 1;
      scrollSequenceRef.current = sequence;

      cancelChatJumpRef.current?.();
      const root = rootRef.current;
      if (!root) return;
      let frame = 0;
      let highlighted: HTMLElement | null = null;
      const observed = new Set<Element>();
      const resize = new ResizeObserver(() => schedule());
      const align = () => {
        frame = 0;
        if (scrollSequenceRef.current !== sequence) return;
        const container = root.querySelector<HTMLElement>(runtimeScrollContainerSelector);
        if (!container) return;
        // Observe message heights, not the fixed-height scrolling viewport.
        for (const child of container.children) {
          if (!observed.has(child)) {
            resize.observe(child);
            observed.add(child);
          }
        }
        for (const child of observed) {
          if (!container.contains(child)) {
            resize.unobserve(child);
            observed.delete(child);
          }
        }
        const target = getMessageElementInScrollContainer(container, `request-${chatId}`);
        if (!target) return;
        scrollTargetIntoContainerTop(
          container,
          target,
          'auto',
          getScrollVisibleTopOffset(container),
        );
        if (highlighted !== target) {
          highlighted?.classList.remove(runtimeAnchorActiveClass);
          highlighted = target;
          target.classList.add(runtimeAnchorActiveClass);
          if (anchorHighlightTimerRef.current !== null)
            window.clearTimeout(anchorHighlightTimerRef.current);
          anchorHighlightTimerRef.current = window.setTimeout(() => {
            target.classList.remove(runtimeAnchorActiveClass);
            anchorHighlightTimerRef.current = null;
          }, runtimeAnchorActiveDuration);
        }
      };
      const schedule = () => {
        if (!frame) frame = window.requestAnimationFrame(align);
      };
      const changes = new MutationObserver(schedule);
      const cancel = () => {
        window.cancelAnimationFrame(frame);
        changes.disconnect();
        resize.disconnect();
        delete root.dataset.chatJump;
        root.removeEventListener('wheel', cancel, true);
        root.removeEventListener('touchstart', cancel, true);
        root.removeEventListener('pointerdown', cancel, true);
        root.removeEventListener('keydown', cancel, true);
        cancelChatJumpRef.current = null;
      };
      cancelChatJumpRef.current = cancel;
      root.dataset.chatJump = 'true';
      changes.observe(root, { childList: true, subtree: true, characterData: true });
      root.addEventListener('wheel', cancel, { capture: true, passive: true });
      root.addEventListener('touchstart', cancel, { capture: true, passive: true });
      root.addEventListener('pointerdown', cancel, true);
      root.addEventListener('keydown', cancel, true);
      schedule();
    }, []);

    useEffect(() => () => cancelChatJumpRef.current?.(), [runtimeInputMountKey]);

    useImperativeHandle(
      forwardedRef,
      () => ({
        scrollToChat,
      }),
      [scrollToChat],
    );

    useEffect(
      () => () => {
        scrollSequenceRef.current += 1;
        if (anchorHighlightTimerRef.current !== null) {
          window.clearTimeout(anchorHighlightTimerRef.current);
        }
        if (runtimeInputRestoreFrameRef.current !== null) {
          window.cancelAnimationFrame(runtimeInputRestoreFrameRef.current);
        }
      },
      [],
    );

    useEffect(() => {
      const root = rootRef.current;
      if (!root) return undefined;

      const handleRuntimeInput = (event: Event) => {
        if (
          event.target instanceof HTMLTextAreaElement &&
          event.target.matches('textarea.data-runtime-sender-input')
        ) {
          runtimeInputDraftRef.current = event.target.value;
          cacheRuntimeInputDraft(runtimeInputCacheKeyRef.current, event.target.value);
        }
      };

      root.addEventListener('input', handleRuntimeInput);
      return () => root.removeEventListener('input', handleRuntimeInput);
    }, []);

    useEffect(() => {
      runtimeInputCacheKeyRef.current = runtimeInputCacheKey;
      const draft = getRuntimeInputDraft(runtimeInputCacheKey);
      runtimeInputDraftRef.current = draft;

      if (runtimeInputRestoreFrameRef.current !== null) {
        window.cancelAnimationFrame(runtimeInputRestoreFrameRef.current);
      }
      runtimeInputRestoreFrameRef.current = window.requestAnimationFrame(() => {
        runtimeInputRestoreFrameRef.current = null;
        const textarea = runtimeInputTextarea(rootRef.current);
        const draft = runtimeInputDraftRef.current;
        if (!textarea || !draft || textarea.value) return;
        setRuntimeInputValue(textarea, draft);
      });
    }, [runtimeInputCacheKey, runtimeInputMountKey]);

    useEffect(() => {
      const nextLifecycle = {
        connectionStatus: controller.connectionStatus,
        sessionId: runtimeSession?.id,
      };
      const previousLifecycle = runtimeInputLifecycleRef.current;
      runtimeInputLifecycleRef.current = nextLifecycle;
      const wasStreaming = ['connecting', 'streaming'].includes(previousLifecycle.connectionStatus);
      const streamSettled = ['eof', 'aborted', 'error'].includes(nextLifecycle.connectionStatus);

      if (
        !wasStreaming ||
        !streamSettled ||
        previousLifecycle.sessionId !== nextLifecycle.sessionId
      ) {
        return;
      }

      if (runtimeInputRestoreFrameRef.current !== null) {
        window.cancelAnimationFrame(runtimeInputRestoreFrameRef.current);
      }
      runtimeInputRestoreFrameRef.current = window.requestAnimationFrame(() => {
        runtimeInputRestoreFrameRef.current = null;
        const textarea = runtimeInputTextarea(rootRef.current);
        const draft = runtimeInputDraftRef.current;
        // AgentScope clears its controlled input after awaiting the full SSE.
        // Restore only a newer user draft, and never overwrite live input.
        if (!textarea || !draft || textarea.value) return;
        setRuntimeInputValue(textarea, draft);
      });
    }, [controller.connectionStatus, runtimeSession?.id]);

    const needsInputHeight = Boolean(pendingClarification) || hasToolConfirmations;
    useEffect(() => {
      const root = rootRef.current;
      if (!root || !needsInputHeight || typeof ResizeObserver === 'undefined') return;
      let observedInput: HTMLElement | null = null;
      let previousHeight = 0;
      const publishHeight = (height: number) => {
        const next = Math.ceil(height);
        if (Number.isFinite(next) && next > 0 && next !== previousHeight) {
          previousHeight = next;
          root.style.setProperty(runtimeInputHeightProperty, `${next}px`);
        }
      };
      const resizeObserver = new ResizeObserver((entries) => {
        const entry = entries[0];
        if (entry) publishHeight(entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height);
      });
      // Observe the tree only until the SDK mounts the input. Message streaming
      // must not query/measure the input for every descendant DOM mutation.
      const discovery = new MutationObserver(() => attach());
      const attach = () => {
        observedInput = root.querySelector<HTMLElement>(runtimeInputSelector);
        if (!observedInput) return;
        resizeObserver.observe(observedInput);
        discovery.disconnect();
      };
      discovery.observe(root, { childList: true, subtree: true });
      attach();
      return () => {
        discovery.disconnect();
        resizeObserver.disconnect();
        root.style.removeProperty(runtimeInputHeightProperty);
      };
    }, [needsInputHeight, runtimeInputMountKey]);

    const runtimeInputQueueSessionId = resolveRuntimeInputQueueSessionId({
      runtimeSessionId: controlledRuntimeSessionId,
      persistedSessionId: controller.session?.id,
      draftQueueSessionId: controller.inputQueueSessionId,
    });
    const runtimeInputQueueHasItems = hasInputQueueItems(runtimeInputQueueSessionId);

    useEffect(() => {
      setControlledRuntimeSessionId((currentSessionId) =>
        selectControlledRuntimeSessionId({
          currentSessionId,
          nextSessionId: runtimeSession?.id,
          connectionStatus: controller.connectionStatus,
          hasPendingInput: Boolean(controller.session && !controller.currentChatId),
        }),
      );
    }, [
      controller.connectionStatus,
      controller.currentChatId,
      controller.session,
      runtimeSession?.id,
    ]);

    useEffect(() => {
      const runtime = runtimeRef.current;
      if (!runtime || !controller.runtimeSession) return;

      const previousSessionId = syncedRuntimeSessionIdRef.current;
      const wasGenerating = runtimeSessionGeneratingRef.current;
      const isGenerating = Boolean(controller.runtimeSession.generating);
      const syncMode = selectRuntimeMessageSyncMode({
        previousSessionId,
        nextSessionId: controller.runtimeSession.id,
        wasGenerating,
        isGenerating,
        connectionStatus: controller.connectionStatus,
        hasQueuedInputs: runtimeInputQueueHasItems,
      });

      // AgentScope's createSession flow owns the optimistic request while the
      // controlled route catches up from the local draft id to the persisted id.
      // Keep that pair mounted until the first response settles.
      if (syncMode === 'none') {
        runtimeSessionGeneratingRef.current = isGenerating;
        syncedRuntimeSessionIdRef.current = controller.runtimeSession.id;
        return;
      }

      const frame = window.requestAnimationFrame(() => {
        if (!isCurrentRuntime() || runtimeRef.current !== runtime) return;
        // Commit sync bookkeeping only after the scheduled replacement runs.
        // Otherwise an effect cleanup can cancel the frame and lose the update.
        runtimeSessionGeneratingRef.current = isGenerating;
        syncedRuntimeSessionIdRef.current = controller.runtimeSession?.id ?? null;
        backgroundQueueMessageIdsRef.current.clear();
        runtime.messages.removeAllMessages();
        controller.runtimeSession?.messages.forEach((message) => {
          runtime.messages.updateMessage(message);
        });
      });
      return () => window.cancelAnimationFrame(frame);
    }, [
      controller.connectionStatus,
      controller.runtimeSession,
      isCurrentRuntime,
      runtimeInputQueueHasItems,
    ]);

    useEffect(() => {
      runtimeRef.current?.input.setDisabled(
        controller.readOnly || controller.connectionStatus === 'loading',
      );
    }, [controller.connectionStatus, controller.readOnly, runtimeInputMountKey]);

    const hydratedVersionRef = useRef<{ mountKey: string; version: number } | null>(null);
    useEffect(() => {
      const runtime = runtimeRef.current;
      const session = controller.runtimeSession;
      if (!runtime || !session || controlledRuntimeSessionId !== session.id) return;
      const next = { mountKey: runtimeInputMountKey, version: controller.historyVersion };
      const shouldHydrate = shouldHydrateRuntimeHistory(hydratedVersionRef.current, next);
      hydratedVersionRef.current = next;
      // Mounts and draft-to-persisted transitions belong to the SDK. Only an
      // actual history/trace update may reconcile its optimistic messages.
      if (!shouldHydrate) return;
      const scroll = rootRef.current?.querySelector<HTMLElement>(runtimeScrollContainerSelector);
      const top = scroll?.getBoundingClientRect().top ?? 0;
      let anchor: { id: string; top: number } | undefined;
      if (scroll) {
        const bottom = top + scroll.clientHeight;
        for (const element of scroll.querySelectorAll<HTMLElement>('[data-role][id]')) {
          const rect = element.getBoundingClientRect();
          if (rect.bottom > top && rect.top < bottom && (!anchor || rect.top < anchor.top)) {
            anchor = { id: element.id, top: rect.top };
            break;
          }
        }
      }
      const anchorTop = anchor?.top;
      const previousScrollTop = scroll?.scrollTop ?? 0;
      const previousHeight = scroll?.scrollHeight ?? 0;
      const reversed = scroll && getComputedStyle(scroll).flexDirection === 'column-reverse';
      const protectedIds = new Set(
        controller.projections
          .filter(({ chat }) => chat.status === 'running')
          .flatMap(({ chat }) => [`request-${chat.id}`, `assistant-${chat.id}`]),
      );
      runtime.messages.setSessionMessages(controlledRuntimeSessionId, (mounted) =>
        mergeHydratedRuntimeMessages({
          mounted,
          snapshot: session.messages,
          protectedIds,
        }),
      );
      // The SDK pagination switch prevents local history expansion from racing
      // backend pagination. Restore the visible anchor once after reconciliation.
      let interacted = false;
      const onInteraction = () => {
        interacted = true;
      };
      const interactionEvents = ['wheel', 'touchmove', 'pointerdown', 'keydown'] as const;
      interactionEvents.forEach((type) =>
        scroll?.addEventListener(type, onInteraction, { passive: true }),
      );
      const clearInteraction = () =>
        interactionEvents.forEach((type) => scroll?.removeEventListener(type, onInteraction));
      const frame = window.requestAnimationFrame(() => {
        clearInteraction();
        if (
          interacted ||
          Date.now() < manualScrollUntilRef.current ||
          !scroll ||
          rootRef.current?.dataset.chatJump === 'true'
        )
          return;
        const element = anchor && getMessageElementInScrollContainer(scroll, anchor.id);
        if (element && anchorTop !== undefined) {
          scroll.scrollTop += element.getBoundingClientRect().top - anchorTop;
        } else {
          scroll.scrollTop =
            previousScrollTop + (reversed ? 0 : scroll.scrollHeight - previousHeight);
        }
      });
      return () => {
        clearInteraction();
        window.cancelAnimationFrame(frame);
      };
    }, [controller.historyVersion, controlledRuntimeSessionId, runtimeInputMountKey]);

    const sessionApi = useMemo<IAgentScopeRuntimeWebUISessionAPI>(
      () => ({
        getSessionList: async () => (runtimeSession ? [runtimeSession] : []),
        getSession: async () => {
          if (!isCurrentRuntime()) throw new DOMException('Session changed', 'AbortError');
          if (!runtimeSession) throw new Error(t('runtime.sessionNotLoaded'));
          const mountedMessages = runtimeRef.current?.messages.getMessages();
          return {
            ...runtimeSession,
            messages: [
              ...selectRuntimeSessionMessages({
                snapshotMessages: runtimeSession.messages,
                mountedMessages,
                snapshotSessionId: runtimeSession.id,
                mountedSessionId: controlledRuntimeSessionId,
                hasQueuedInputs: runtimeInputQueueHasItems,
              }),
            ],
          };
        },
        updateSession: async (patch) => {
          if (!isCurrentRuntime()) return [];
          if (
            patch.id &&
            patch.id !== runtimeSession?.id &&
            patch.id !== controlledRuntimeSessionId
          ) {
            return [];
          }
          const runtime = runtimeRef.current;
          if (runtime && patch.messages) {
            // The SDK background queue persists through sessionApi without touching
            // the mounted message handler. Reconcile only queue-owned messages so
            // the just-finished SSE response stays visible across the handoff.
            const reconciliation = reconcileBackgroundRuntimeMessages({
              currentMessages: runtime.messages.getMessages(),
              nextMessages: patch.messages,
              trackedMessageIds: backgroundQueueMessageIdsRef.current,
            });
            reconciliation.removedMessageIds.forEach((messageId) => {
              runtime.messages.removeMessage({ id: messageId });
            });
            reconciliation.upsertMessages.forEach((item) => {
              runtime.messages.updateMessage(item);
            });
            backgroundQueueMessageIdsRef.current = reconciliation.trackedMessageIds;
          }
          return runtimeSession ? [{ ...runtimeSession, ...patch }] : [];
        },
        createSession: async () => (runtimeSession ? [runtimeSession] : []),
        removeSession: async () => (runtimeSession ? [runtimeSession] : []),
      }),
      [controlledRuntimeSessionId, isCurrentRuntime, runtimeInputQueueHasItems, runtimeSession, t],
    );

    const pendingAdmissionRef = useRef(false);
    const submitPendingComments = useCallback(
      async (inputText: string) => {
        const runtime = runtimeRef.current;
        const comments = pendingCommentsRef.current;
        if (attachmentUploadsInFlightRef.current.size) {
          message.warning(t('runtime.attachmentUploading'));
          return;
        }
        if (
          !runtime ||
          (!comments.length && !getRuntimeInputAttachments(runtimeInputCacheKey).length)
        ) {
          if (!runtime) message.warning(t('runtime.inputNotReady'));
          return;
        }
        if (pendingAdmissionRef.current) return;
        pendingAdmissionRef.current = true;
        try {
          const assertAccess = controller.captureWriteAccess();
          modelAdmissionRef.current = undefined;
          modelAdmissionRef.current = await controller.ensureModelConfiguration();
          assertAccess();
          if (
            !rootRef.current ||
            runtimeInputCacheKey !== runtimeInputCacheKeyRef.current ||
            runtime !== runtimeRef.current ||
            comments !== pendingCommentsRef.current
          )
            return;
        } catch {
          return;
        } finally {
          pendingAdmissionRef.current = false;
        }
        const text =
          inputText.trim() ||
          (comments.length ? t('artifact.commentOnlyPrompt') : t('review.returnPrompt'));
        pendingCommentsRef.current = EMPTY_PENDING_COMMENTS;
        onConsumePendingComments?.();
        const data: IAgentScopeRuntimeWebUIInputData = {
          query: text,
          biz_params: artifactCommentsBizParams(comments),
        };
        preparePendingAttachments(data);
        runtime.input.submit(data);
        clearRuntimeInputAfterAccepted(rootRef.current, inputText);
      },
      [controller, onConsumePendingComments, preparePendingAttachments, runtimeInputCacheKey, t],
    );
    pendingSubmitRef.current = submitPendingComments;

    const handleKeyDownCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Enter' && event.altKey) {
        // AgentScope's queue capture treats every non-Shift Enter as submit.
        // Keep Option/Alt+Enter available as the second documented newline gesture.
        event.stopPropagation();
        return;
      }
      if (
        event.key === 'Enter' &&
        !event.shiftKey &&
        !(event.nativeEvent as KeyboardEvent).isComposing &&
        event.target instanceof HTMLTextAreaElement &&
        (pendingCommentsRef.current.length ||
          (!event.currentTarget.querySelector('textarea')?.value.trim() &&
            getRuntimeInputAttachments(runtimeInputCacheKey).length))
      ) {
        event.preventDefault();
        event.stopPropagation();
        submitPendingComments(event.target.value);
      }
    };

    const queueItemActions = useMemo<readonly IAgentScopeRuntimeWebUIQueueItemAction[]>(
      () => [
        {
          key: 'steer-current-chat',
          label: ({ item }) =>
            item.data.query?.trim() ? t('runtime.steer') : t('runtime.attachmentCannotSteer'),
          icon: ({ item }) =>
            steeringItemId === item.id ? <LoadingOutlined spin /> : <CompassOutlined />,
          visible: isRuntimeQueueSteerVisible(currentChatStatus),
          ownerOnly: runtimeQueueSteerOwnerOnly,
          disabled: ({ item }) =>
            !item.data.query?.trim() || (steeringItemId !== null && steeringItemId !== item.id),
          onClick: async ({ item, remove }) => {
            const text = item.data.query?.trim();
            if (!text) {
              message.warning(t('runtime.attachmentCannotGuide'));
              return;
            }
            setSteeringItemId(item.id);
            try {
              const comments = artifactCommentsFromBizParams(item.data.biz_params);
              await steerCurrentChat(text, comments.map(toArtifactComment));
              remove();
              message.success(t('runtime.steerSuccess'));
            } catch (error) {
              message.error(error instanceof Error ? error.message : t('runtime.steerFailed'));
            } finally {
              setSteeringItemId(null);
            }
          },
        },
      ],
      [currentChatStatus, steerCurrentChat, steeringItemId, t],
    );

    const runtimeInputQueueEnabled = !controller.readOnly && shouldUseRuntimeInputQueue();

    const [getResponseSections] = useState(() => {
      const cache = new WeakMap<
        ConsoleChatController['projections'][number],
        RuntimeResponseSection[]
      >();
      return (projection: ConsoleChatController['projections'][number]) => {
        const previous = cache.get(projection);
        if (previous) return previous;
        const sections: RuntimeResponseSection[] = splitChatTimelineBySteer(
          selectChatTimeline(projection),
        );
        if (!sections.length)
          sections.push({
            kind: 'response',
            id: projection.responseId ?? `response-${projection.chat.id}`,
            items: [],
          });
        cache.set(projection, sections);
        return sections;
      };
    });

    const options = useMemo<IAgentScopeRuntimeWebUIOptions>(
      () => ({
        theme: {
          ...createRuntimeThemeOptions(colorMode),
          locale: language === 'zh' ? 'cn' : 'en',
          bubbleList: {
            pagination: false,
            userMessageAnchors: {
              enabled: false,
            },
          },
        },
        welcome: {
          nick: 'Data Analyst',
          greeting: t('runtime.startAnalysis'),
          description: t('runtime.startDescription'),
          avatar: `${import.meta.env.BASE_URL}mascot-blue-transparent.png`,
        },
        sender: {
          enableFocusExpand: true,
          placeholder: t('runtime.placeholder'),
          maxLength: 4000,
          showCharacterCount: false,
          beforeSubmit: beforeRuntimeSubmit,
          attachments: {
            accept: [...ATTACHMENT_EXTENSIONS].map((item) => `.${item}`).join(','),
            multiple: true,
            beforeUpload: beforeAttachmentUpload,
            customRequest: uploadAttachment,
            onChange: ({ fileList }) => {
              liveUploads.attachments = fileList.flatMap((file) => {
                const attachment = parseAttachmentReferenceUrl(file.response?.url);
                return attachment ? [attachment] : [];
              });
            },
          },
          beforeUI: !controller.readOnly ? (
            <>
              {contextUsageOpen ? (
                <ContextUsagePanel
                  key={runtimeInputCacheKey}
                  usage={contextUsage}
                  onClose={() => setContextUsageOpenFor(null)}
                />
              ) : null}
              {composerNotice}
              {pendingAttachments.length ? (
                <div className={styles.pendingInputAttachments}>
                  {pendingAttachments.map((attachment) => (
                    <span className={styles.pendingInputAttachment} key={attachment.attachment_id}>
                      <FileOutlined aria-hidden="true" />
                      <span title={attachment.filename}>{attachment.filename}</span>
                      <Button
                        type="text"
                        size="small"
                        icon={<DeleteOutlined />}
                        aria-label={t('review.removeAttachment', { filename: attachment.filename })}
                        onClick={() =>
                          consumeRuntimeInputAttachments(runtimeInputCacheKey, [
                            attachment.attachment_id,
                          ])
                        }
                      />
                    </span>
                  ))}
                </div>
              ) : null}
              {pendingComments.length ? (
                <PendingArtifactComments
                  comments={pendingComments}
                  key="runtime-pending-comments"
                  rootRef={rootRef}
                  onClearComments={onConsumePendingComments}
                  onOpenComment={onOpenComment}
                />
              ) : null}
            </>
          ) : undefined,
          // Keep the SDK queue controller mounted from the first direct request so
          // a later enqueue during SSE retains the active request's ownership.
          // The empty queue renders no panel, and isSessionRunning still lets the
          // first request create its backend Session through the direct path.
          queue: runtimeInputQueueEnabled
            ? {
                enable: true,
                maxSize: 20,
                itemActions: queueItemActions,
                getQueueKey: (runtimeSessionId) =>
                  resolveRuntimeInputQueueSessionId({
                    runtimeSessionId,
                    persistedSessionId: controller.session?.id,
                    draftQueueSessionId: controller.inputQueueSessionId,
                  }),
                getRequestContext: () =>
                  controller.session
                    ? {
                        session_id: controller.session.id,
                        channel: 'main',
                        agent_id: controller.session.agent_id,
                      }
                    : undefined,
                isSessionRunning: controller.isCurrentChatRunning,
                onInputEnqueued: () => {
                  modelAdmissionRef.current = undefined;
                },
                shouldRestoreOnError: () => true,
                onFull: () => {
                  restorePendingSubmission();
                  message.warning(t('review.queueFull'));
                },
                onSessionNotReady: () => {
                  restorePendingSubmission();
                  message.warning(t('review.sessionNotReady'));
                },
              }
            : false,
          prefix: (
            <div className={styles.runtimeInputTools}>
              <RuntimeDataSelectors controller={controller} />
            </div>
          ),
          actionAffix: ({ disabled, value }) => (
            <>
              <div className={styles.runtimeQuickActions}>
                <Tooltip title={t('runtime.coffeeModeTooltip')}>
                  <Button
                    aria-label={t('runtime.coffeeMode')}
                    aria-pressed={coffeeModeEnabled}
                    className={styles.runtimeCoffeeButton}
                    icon={<Coffee size={16} aria-hidden="true" />}
                    disabled={controller.readOnly}
                    onClick={() => {
                      void controller.setEnableClarification(coffeeModeEnabled).catch((error) => {
                        message.error(error instanceof Error ? error.message : String(error));
                      });
                    }}
                    type="text"
                  />
                </Tooltip>
                <ContextUsageButton
                  usage={contextUsage}
                  open={contextUsageOpen}
                  onClick={() =>
                    setContextUsageOpenFor(contextUsageOpen ? null : runtimeInputCacheKey)
                  }
                />
              </div>
              {shouldShowRuntimeStopButton(controller.currentProjection?.chat.status, value) ? (
                <Tooltip title={t('runtime.stop')}>
                  <Button
                    aria-label={t('runtime.stop')}
                    className={styles.runtimeStopButton}
                    data-runtime-stop-button="true"
                    onClick={controller.stopCurrentChat}
                    type="primary"
                  >
                    <span aria-hidden="true" className={styles.runtimeStopGlyph} />
                  </Button>
                </Tooltip>
              ) : null}
              {pendingComments.length || (pendingAttachments.length && !value.trim()) ? (
                <Tooltip
                  title={t(
                    pendingComments.length ? 'artifact.commentSend' : 'review.sendAttachments',
                  )}
                >
                  <Button
                    aria-label={t(
                      pendingComments.length ? 'artifact.commentSend' : 'review.sendAttachments',
                    )}
                    className={styles.pendingArtifactCommentSubmit}
                    data-pending-comment-submit="true"
                    disabled={Boolean(disabled)}
                    icon={<SendOutlined />}
                    onClick={() => submitPendingComments(value)}
                    type="primary"
                  />
                </Tooltip>
              ) : null}
            </>
          ),
        },
        session: {
          multiple: false,
          hideBuiltInSessionList: true,
          currentSessionId: controlledRuntimeSessionId,
          api: sessionApi,
        },
        api: {
          fetch: async ({ biz_params: bizParams, input, signal, submission }) => {
            const text = extractLatestRuntimeText(input);
            const attachments = extractLatestRuntimeAttachments(input);
            if (!text && !attachments.length) {
              throw new Error(t('runtime.questionRequired'));
            }
            const comments = artifactCommentsFromBizParams(bizParams);
            try {
              if (!isCurrentRuntime()) throw new DOMException('Session changed', 'AbortError');
              controller.captureWriteAccess();
              // Queue admission freezes its target; never submit it to the newly mounted session.
              if (
                bizParams?.session_id &&
                bizParams.session_id !== controller.session?.id &&
                bizParams.session_id !== runtimeScope.sessionId &&
                bizParams.session_id !== controller.inputQueueSessionId
              ) {
                throw new DOMException('Session changed', 'AbortError');
              }
              const admission =
                submission?.source === 'direct' ? modelAdmissionRef.current : undefined;
              modelAdmissionRef.current = undefined;
              const response = await controller.submitRuntimeRequest(
                text,
                signal,
                attachments.map((item) => item.attachment_id),
                comments.map(toArtifactComment),
                admission,
              );
              // Creating the first backend Session changes runtimeScope.sessionId
              // while the same input remains mounted. Acceptance still belongs to
              // that input; an actual session switch/remount must remain isolated.
              if (activeRuntimeScopeRef.current.mountKey === runtimeScope.mountKey) {
                if (shouldClearAcceptedRuntimeInput(runtimeInputDraftRef.current, text)) {
                  runtimeInputDraftRef.current = '';
                }
                clearAcceptedRuntimeInputDraft(runtimeInputCacheKeyRef.current, text);
                clearRuntimeInputAfterAccepted(rootRef.current, text, attachments.length);
              }
              return response;
            } catch (error) {
              if (submission?.source === 'direct') {
                // IDs were frozen at input admission, before a queue or session switch.
                const ids = bizParams?.user_prompt_params?.qwenpaw_data_pending_attachment_ids
                  ? restoredAttachmentIds(
                      bizParams.user_prompt_params.qwenpaw_data_pending_attachment_ids,
                    )
                  : attachments.map((item) => item.attachment_id);
                cacheRuntimeInputAttachments(
                  runtimeInputCacheKey,
                  attachments.filter((item) => ids.includes(item.attachment_id)),
                );
                if (!getRuntimeInputDraft(runtimeInputCacheKey))
                  cacheRuntimeInputDraft(runtimeInputCacheKey, text);
                if (isCurrentRuntime()) {
                  const textarea = runtimeInputTextarea(rootRef.current);
                  if (textarea && !textarea.value) setRuntimeInputValue(textarea, text);
                }
              }
              if (isCurrentRuntime() && submission?.source === 'direct' && comments.length) {
                pendingCommentsRef.current = comments;
                onRestorePendingComments?.(comments);
              }
              throw error;
            }
          },
          responseParser: (chunk) => parseRuntimeStreamChunk(String(chunk)),
          reconnect: ({ session_id: sessionId, signal }) =>
            controller.reconnectRuntimeRequest(sessionId, signal),
          cancel: () => {
            if (isCurrentRuntime()) controller.stopCurrentChat();
          },
        },
        customToolRenderConfig: runtimeToolRenderConfig,
        request: {
          render: ({ data, fallback }) => {
            const attachments = resolveRequestAttachments(
              data,
              controller.projections.map((item) => item.chat),
            );
            const text = selectRequestInputText(data);
            const comments = resolveRequestArtifactComments(
              data,
              controller.projections.map((item) => item.chat),
            );
            const visibleText = comments.length ? visibleArtifactCommentText(text) : text;
            const displayText = visibleText === t('artifact.commentOnlyPrompt') ? '' : visibleText;
            return (
              <article className={styles.runtimeRequestShell}>
                <div className={styles.runtimeRequestColumn}>
                  {text ? (
                    <div className={styles.runtimeRequestBody}>
                      {comments.length ? (
                        <div className="data-runtime-bubble-content data-runtime-bubble-content-filled">
                          <ArtifactCommentChips comments={comments} onOpenComment={onOpenComment} />
                          {displayText ? (
                            <div className={styles.artifactCommentMessageText}>{displayText}</div>
                          ) : null}
                        </div>
                      ) : attachments.length ? (
                        <div className="data-runtime-bubble-content data-runtime-bubble-content-filled">
                          {text}
                        </div>
                      ) : (
                        fallback()
                      )}
                    </div>
                  ) : attachments.length ? null : (
                    <div className={styles.runtimeRequestBody}>{fallback()}</div>
                  )}
                  <RuntimeRequestAttachments attachments={attachments} />
                </div>
                <div
                  className={`${styles.runtimeMessageAvatar} ${styles.runtimeUserAvatar}`}
                  aria-hidden="true"
                >
                  <UserOutlined />
                </div>
              </article>
            );
          },
        },
        response: {
          render: ({ data }) => {
            const projection = controller.projections.find(
              (item) => item.responseId === data.id || `response-${item.chat.id}` === data.id,
            );
            const running = isRuntimeResponseRunning(projection?.chat.status, data.status);
            const chatId = projection?.chat.id;
            const sections: RuntimeResponseSection[] = projection
              ? getResponseSections(projection)
              : [{ kind: 'response', id: data.id, items: null }];
            if (!sections.length) {
              sections.push({ kind: 'response', id: data.id, items: [] });
            }
            let lastResponseIndex = -1;
            sections.forEach((section, index) => {
              if (section.kind === 'response') lastResponseIndex = index;
            });

            return (
              <>
                {sections.map((section, index) => {
                  if (section.kind === 'steer') {
                    return (
                      <RuntimeSteerMessage
                        comments={section.comments}
                        key={section.id}
                        onOpenComment={onOpenComment}
                        text={section.text}
                      />
                    );
                  }
                  const showFooter = index === lastResponseIndex && !running && Boolean(chatId);
                  return (
                    <RuntimeResponseMessage
                      key={section.id}
                      chatId={chatId}
                      sessionId={controller.session?.id}
                      ensureChatTrace={controller.ensureChatTrace}
                      showConfidence={controller.session?.agent_id !== 'review'}
                      failure={
                        index === lastResponseIndex
                          ? selectResponseFailure(projection?.chat, data)
                          : null
                      }
                      readOnly={controller.readOnly}
                      messages={data.output}
                      timeline={section.items}
                      terminal={projection ? projection.chat.status !== 'running' : !running}
                      initialHistoricalSegmentKeys={
                        controller.initialHistoricalSegmentKeys ?? EMPTY_SEGMENT_ID_SET
                      }
                      lastSessionSegmentKey={lastSessionSegmentKey}
                      running={running && index === lastResponseIndex}
                      waitingForInput={Boolean(
                        pendingClarification &&
                        chatId === controller.currentChatId &&
                        index === lastResponseIndex,
                      )}
                      beforeSteer={sections[index + 1]?.kind === 'steer'}
                      afterSteer={sections[index - 1]?.kind === 'steer'}
                      onArtifactOpen={onArtifactOpen}
                      onArtifactDownload={onArtifactDownload}
                      onArtifactShare={onArtifactShare}
                      footer={
                        showFooter && chatId ? (
                          <>
                            <RuntimeResponseActions
                              readOnly={controller.readOnly}
                              chatId={chatId}
                              chatStatus={projection?.chat.status}
                              text={serializeRuntimeResponseToMarkdown(data.output)}
                              forkable={Boolean(projection?.chat.forkable)}
                              forkDisabled={controller.currentProjection?.chat.status === 'running'}
                              onFeedback={(kind, detail) =>
                                controller.submitFeedback(chatId, kind, detail)
                              }
                              onFork={controller.forkChat}
                              loadReaction={controller.getFeedbackReaction}
                              clearReaction={controller.clearFeedbackReaction}
                              onCreateSkillDraft={onCreateSkillDraft}
                            />
                            {!controller.readOnly ? (
                              <RuntimeFollowupHints
                                questions={
                                  controller.projections.find(({ chat }) => chat.id === chatId)
                                    ?.chat.followup?.questions ?? []
                                }
                                onSelect={(question) =>
                                  fillRuntimeInput(
                                    rootRef.current,
                                    question,
                                    t('runtime.inputNotReady'),
                                  )
                                }
                              />
                            ) : null}
                          </>
                        ) : null
                      }
                    />
                  );
                })}
              </>
            );
          },
        },
        actions: {
          list: [],
          right: false,
          replace: false,
        },
        requestActions: {
          list: [],
        },
      }),
      [
        getResponseSections,
        contextUsage,
        contextUsageOpen,
        runtimeInputCacheKey,
        controlledRuntimeSessionId,
        lastSessionSegmentKey,
        colorMode,
        controller,
        language,
        onArtifactDownload,
        onArtifactOpen,
        onArtifactShare,
        onCreateSkillDraft,
        beforeAttachmentUpload,
        beforeRuntimeSubmit,
        coffeeModeEnabled,
        onConsumePendingComments,
        onOpenComment,
        onRestorePendingComments,
        pendingClarification,
        pendingComments,
        queueItemActions,
        runtimeInputQueueEnabled,
        runtimeScope,
        sessionApi,
        isCurrentRuntime,
        submitPendingComments,
        t,
        uploadAttachment,
        composerNotice,
        pendingAttachments,
        liveUploads,
        restorePendingSubmission,
      ],
    );

    return (
      <div
        className={styles.runtimeConversation}
        onWheelCapture={() => {
          manualScrollUntilRef.current = Date.now() + 180;
        }}
        onTouchMoveCapture={() => {
          manualScrollUntilRef.current = Date.now() + 180;
        }}
        data-has-clarification={pendingClarification ? 'true' : 'false'}
        data-has-pending-comments={pendingComments.length ? 'true' : 'false'}
        data-new-session={controller.session ? 'false' : 'true'}
        data-read-only={controller.readOnly ? 'true' : 'false'}
        data-runtime-webui="true"
        data-timeline-items={controller.currentTimeline.length}
        onKeyDownCapture={handleKeyDownCapture}
        onPointerOverCapture={(event) => {
          if (!(event.target instanceof Element)) return;
          const actions = event.target.closest<HTMLElement>(
            '.data-runtime-sender-actions-list-presets',
          );
          if (!actions) return;
          // Put the native tooltip on the wrapper so disabled send buttons also show it.
          if (actions.querySelector('button .anticon-arrow-up')) {
            actions.title = t('runtime.shortcut');
          } else {
            actions.removeAttribute('title');
          }
        }}
        onPointerOutCapture={(event) => {
          if (!(event.target instanceof Element)) return;
          const actions = event.target.closest<HTMLElement>(
            '.data-runtime-sender-actions-list-presets',
          );
          if (
            actions &&
            (!(event.relatedTarget instanceof Node) || !actions.contains(event.relatedTarget))
          ) {
            actions.removeAttribute('title');
          }
        }}
        ref={rootRef}
      >
        {controller.historyLoading ? <HistoryLoading /> : null}
        {controller.historyError ? (
          <div className={styles.runtimeConnectionNotice} role="alert">
            <span>{controller.historyError}</span>
            <Button size="small" onClick={() => void controller.loadOlderChats()}>
              {t('console.loadOlderChats')}
            </Button>
          </div>
        ) : null}
        {controller.error ? (
          <div className={styles.runtimeConnectionNotice} role="alert">
            <span>{controller.error}</span>
            {controller.modelConfigurationRequired && onOpenAgentConfiguration ? (
              <Button size="small" type="link" onClick={onOpenAgentConfiguration}>
                {t('runtime.configureAgent')}
              </Button>
            ) : null}
          </div>
        ) : null}
        {controller.cmModelConfigurationRequired ? (
          <div className={styles.runtimeConnectionNotice} role="alert">
            <span>{t('console.cmModelConfigurationRequired')}</span>
            {onOpenDataBridgeModelConfig ? (
              <Button size="small" type="link" onClick={onOpenDataBridgeModelConfig}>
                {t('runtime.configureDataBridgeModels')}
              </Button>
            ) : null}
          </div>
        ) : null}
        <AntConfigProvider prefixCls="data-runtime" theme={runtimeAntTheme}>
          {/* The SDK stores loading by Runtime Session ID and blocks queue draining
            during a route change. Remount for each draft, when `new` becomes the
            persisted Session, and when Snapshot discovers a backend-created Chat,
            so messages and SSE reconnect from the host's authoritative state. */}
          <AgentScopeRuntimeWebUI key={runtimeInputMountKey} ref={runtimeRef} options={options} />
        </AntConfigProvider>
        <div className={hasToolConfirmations ? clarificationStyles.clarificationDock : undefined}>
          <div className={hasToolConfirmations ? styles.runtimeDecisionContent : undefined}>
            {hasToolConfirmations && confirmationState.recoveryLimited ? (
              <p className={styles.confirmationRecoveryNotice} role="status">
                {t('toolConfirmation.recoveryLimited')}
              </p>
            ) : null}
            {visibleConfirmations.map((confirmation) => (
              <RuntimeToolConfirmationCard
                key={`${controller.accessVersion}:${confirmation.chatId}:${confirmation.callId}`}
                confirmation={confirmation}
                submission={
                  confirmationState.records[
                    confirmationKey({
                      sessionId: controller.session?.id ?? '',
                      chatId: confirmation.chatId,
                      callId: confirmation.callId,
                    })
                  ]
                }
                status={controller.currentProjection!.chat.status}
                ready={controller.toolConfirmationReady}
                readOnly={controller.readOnly}
                stopping={controller.toolConfirmationStopping}
                onDecide={(decision) =>
                  controller.answerToolConfirmation(
                    confirmation.chatId,
                    confirmation.callId,
                    decision,
                  )
                }
              />
            ))}
            {pendingClarification && controller.currentChatId ? (
              <RuntimeClarificationCard
                embedded={hasToolConfirmations}
                key={pendingClarification.id}
                clarification={pendingClarification}
                onClose={() =>
                  resolveClarification(pendingClarification.id, {
                    status: 'answered',
                    answers: buildClarificationCancellationAnswers(pendingClarification.questions),
                  })
                }
                onSubmit={(answers) =>
                  resolveClarification(pendingClarification.id, { status: 'answered', answers })
                }
              />
            ) : null}
          </div>
        </div>
      </div>
    );
  },
);

export default RuntimeConversation;
