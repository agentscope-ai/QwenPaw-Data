import { getRuntimeConfig } from '@/services/runtimeConfig';
import AgentScopeRuntimeWebUI, {
  hasInputQueueItems,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI';
import type {
  IAgentScopeRuntimeWebUIOptions,
  IAgentScopeRuntimeWebUIQueueItemAction,
  IAgentScopeRuntimeWebUISessionAPI,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type { IAgentScopeRuntimeWebUIRef } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types/IRef';
import RuntimeMessage from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Message';
import RuntimeError from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Error';
import type {
  IAgentScopeRuntimeMessage,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  AgentScopeRuntimeMessageType,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  getMessageElementInScrollContainer,
  getScrollVisibleTopOffset,
  scrollTargetIntoContainerTop,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Chat/MessageList/UserMessageAnchors/helpers';
import {
  BranchesOutlined,
  CompassOutlined,
  CopyOutlined,
  DislikeOutlined,
  LikeOutlined,
  LoadingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Select, Tooltip } from '@agentscope-ai/design';
import { runtimeAntTheme, runtimeThemeOptions } from '@/theme';
import {
  ConfigProvider as AntConfigProvider,
  Input as AntInput,
  Modal,
  Radio,
  message,
} from 'antd';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
} from 'react';
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  parseRuntimeStreamChunk,
  timelineItemToRuntimeMessage,
  type ConsoleChatController,
} from '../model/useConsoleChat';
import {
  isAskUserQuestionExpired,
  selectPendingAskUserQuestion,
} from '../model/askUserQuestion';
import { serializeRuntimeResponseToMarkdown } from '../model/markdownContent';
import {
  extractLatestRuntimeText,
  shouldClearAcceptedRuntimeInput,
} from '../model/runtimeRequest';
import {
  isRuntimeQueueSteerVisible,
  runtimeQueueSteerOwnerOnly,
  shouldShowRuntimeStopButton,
  shouldUseRuntimeInputQueue,
} from '../model/runtimeQueuePolicy';
import {
  isRuntimeToolInputMessage,
  isRuntimeToolMessage,
  isRuntimeToolOutputMessage,
  isRuntimeResponseRunning,
  toolMessageKey,
} from '../model/runtimeProcess';
import { selectRuntimeProjectionPayload } from '../model/runtimeProjection';
import {
  selectChatTimeline,
  splitChatTimelineBySteer,
  type ChatTimelineItem,
  type ChatTimelineSection,
} from '../model/selectChatTimeline';
import {
  resolveRuntimeInputQueueSessionId,
  selectControlledRuntimeSessionId,
  selectRuntimeMessageSyncMode,
} from '../model/runtimeSessionSync';
import styles from '../index.module.less';
import type { Artifact } from '../types';
import RuntimeBizEventCard from './RuntimeBizEventCard';
import RuntimeClarificationCard from './RuntimeClarificationCard';
import RuntimeSegmentCard from './RuntimeSegmentCard';
import RuntimeThinkingCard from './RuntimeThinkingCard';
import {
  RuntimeToolCard,
  runtimeToolRenderConfig,
} from './RuntimeToolCard';

interface RuntimeConversationProps {
  controller: ConsoleChatController;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onOpenAgentConfiguration?: () => void;
}

export interface RuntimeConversationHandle {
  scrollToChat: (chatId: string) => void;
}

const runtimeScrollContainerSelector =
  '.data-runtime-chat-anywhere-message-list-bubble-scroll';
const runtimeInputSelector = '.data-runtime-chat-anywhere-input';
const runtimeInputHeightProperty = '--runtime-input-height';
const runtimeAnchorActiveClass =
  'data-runtime-chat-anywhere-message-list-anchor-target-active';
const runtimeAnchorActiveDuration = 1200;
const dislikeReasons = [
  '分析不准确',
  '没有解决问题',
  '内容不完整',
  '表达不清晰',
  '其他',
] as const;

function waitForAnimationFrame(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve());
  });
}

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

function fillRuntimeInput(root: HTMLElement | null, text: string) {
  const textarea = root?.querySelector<HTMLTextAreaElement>('textarea');
  if (!textarea) {
    message.warning('输入框尚未准备好');
    return;
  }
  const valueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    'value',
  )?.set;
  valueSetter?.call(textarea, text);
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  textarea.focus();
  textarea.setSelectionRange(text.length, text.length);
}

function clearRuntimeInputAfterAccepted(
  root: HTMLElement | null,
  submittedText: string,
) {
  const textarea = root?.querySelector<HTMLTextAreaElement>('textarea');
  if (!textarea || !shouldClearAcceptedRuntimeInput(textarea.value, submittedText)) return;

  const valueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    'value',
  )?.set;
  valueSetter?.call(textarea, '');
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
}

type RuntimeDisplayEntry = {
  kind: 'message';
  message: IAgentScopeRuntimeMessage;
};

type RuntimeResponseSection = ChatTimelineSection | {
  kind: 'response';
  id: string;
  items: null;
};

function mergeRuntimeDisplayEntries(
  entries: RuntimeDisplayEntry[],
): RuntimeDisplayEntry[] {
  const result: RuntimeDisplayEntry[] = [];
  const pendingToolIndexes = new Map<string, number>();

  entries.forEach((entry) => {
    const { message: runtimeMessage } = entry;
    if (runtimeMessage.type === AgentScopeRuntimeMessageType.HEARTBEAT) return;

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

function RuntimeProcessTimeline({
  messages,
  timeline,
  terminal,
  onArtifactOpen,
  onArtifactDownload,
}: {
  messages: IAgentScopeRuntimeMessage[];
  timeline?: ChatTimelineItem[] | null;
  terminal: boolean;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
}) {
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
      return [{
          kind: 'message',
          message: timelineItemToRuntimeMessage(item, terminal),
        }];
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
    <div className={styles.runtimeProcessTimeline} aria-label="执行过程">
      {displayEntries.map((entry) => {
        const { message: runtimeMessage } = entry;
        const projection = selectRuntimeProjectionPayload(runtimeMessage);
        if (projection?.kind === 'segment') {
          return (
            <RuntimeSegmentCard
              data={projection.segment}
              index={segmentIndexes.get(runtimeMessage.id) ?? 1}
              key={runtimeMessage.id}
              onArtifactOpen={onArtifactOpen}
              onArtifactDownload={onArtifactDownload}
            />
          );
        }
        if (projection?.kind === 'biz_event') {
          return <RuntimeBizEventCard data={projection.event} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.REASONING) {
          return <RuntimeThinkingCard data={runtimeMessage} key={runtimeMessage.id} />;
        }
        if (isRuntimeToolMessage(runtimeMessage)) {
          return <RuntimeToolCard data={runtimeMessage} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.ERROR) {
          return <RuntimeError data={runtimeMessage} key={runtimeMessage.id} />;
        }
        if (runtimeMessage.type === AgentScopeRuntimeMessageType.MESSAGE) {
          return <RuntimeMessage data={runtimeMessage} key={runtimeMessage.id} />;
        }
        return null;
      })}
    </div>
  );
}

function RuntimeSteerMessage({ text }: { text: string }) {
  return (
    <article className={styles.runtimeSteerRequestShell} aria-label="Steer 引导消息">
      <div className={styles.runtimeSteerRequestBubble}>
        <span className={styles.runtimeSteerRequestIcon} aria-hidden="true">
          <CompassOutlined />
        </span>
        <span>{text}</span>
      </div>
    </article>
  );
}

function RuntimeResponseActions({
  chatId,
  getText,
  onFeedback,
  loadReaction,
  clearReaction,
}: {
  chatId: string;
  getText: () => string;
  onFeedback: (
    kind: 'like' | 'dislike' | 'copy',
    detail?: { reason?: string | null; detail?: string | null },
  ) => Promise<void>;
  loadReaction: (chatId: string) => Promise<'like' | 'dislike' | null>;
  clearReaction: (chatId: string) => Promise<void>;
}) {
  const [reaction, setReaction] = useState<'like' | 'dislike' | null>(null);
  const [reactionLoading, setReactionLoading] = useState(true);
  const [submittingReaction, setSubmittingReaction] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [dislikeOpen, setDislikeOpen] = useState(false);
  const [dislikeReason, setDislikeReason] = useState('');
  const [dislikeDetail, setDislikeDetail] = useState('');
  const [submittingDislike, setSubmittingDislike] = useState(false);

  useEffect(() => {
    let active = true;
    setReactionLoading(true);
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
  }, [chatId, loadReaction]);

  const handleCopy = async () => {
    const text = getText();
    if (!text) return;
    try {
      await writeClipboardText(text);
      setCopyState('copied');
      void onFeedback('copy').catch(() => undefined);
    } catch {
      setCopyState('failed');
    }
    window.setTimeout(() => setCopyState('idle'), 1200);
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
      message.error(error instanceof Error ? error.message : '点赞提交失败');
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
      message.error(error instanceof Error ? error.message : '取消点踩失败');
    } finally {
      setSubmittingReaction(false);
    }
  };

  const submitDislike = async () => {
    if (!dislikeReason) {
      message.warning('请选择需要改进的原因');
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
      message.success('感谢反馈');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '反馈提交失败');
    } finally {
      setSubmittingDislike(false);
    }
  };

  return (
    <footer className={styles.runtimeResponseActions} aria-label="返回消息操作">
      <Tooltip
        title={copyState === 'copied'
          ? '已复制完整 Markdown'
          : copyState === 'failed'
            ? '复制失败，请重试'
            : '复制完整返回内容（Markdown）'}
        trigger={['hover', 'focus']}
      >
        <Button
          type="text"
          icon={<CopyOutlined />}
          aria-label={copyState === 'copied' ? '已复制完整 Markdown' : '复制完整返回内容'}
          onClick={() => void handleCopy()}
        />
      </Tooltip>
      <Tooltip
        title="这个回答有帮助"
        trigger={['hover', 'focus']}
      >
        <Button
          className={reaction === 'like' ? styles.runtimeResponseActionActive : ''}
          type="text"
          icon={<LikeOutlined />}
          aria-label="点赞"
          disabled={reactionLoading || submittingReaction || submittingDislike}
          onClick={() => void handleLike()}
        />
      </Tooltip>
      <Tooltip
        title="这个回答需要改进"
        trigger={['hover', 'focus']}
      >
        <Button
          className={reaction === 'dislike' ? styles.runtimeResponseActionActive : ''}
          type="text"
          icon={<DislikeOutlined />}
          aria-label="点踩"
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
      <Tooltip
        title="从这个返回消息创建分支"
        trigger={['hover', 'focus']}
      >
        <Button
          type="text"
          icon={<BranchesOutlined />}
          aria-label="从这个返回消息创建分支"
          disabled
        />
      </Tooltip>
      <Modal
        open={dislikeOpen}
        title="这个回答哪里需要改进？"
        okText="提交反馈"
        cancelText="取消"
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
              <Radio value={reason} key={reason}>{reason}</Radio>
            ))}
          </Radio.Group>
          <AntInput.TextArea
            rows={4}
            maxLength={500}
            showCount
            value={dislikeDetail}
            placeholder="补充说明（可选）"
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
  if (!questions.length) return null;
  return (
    <div className={styles.followupHints} aria-label="推荐追问">
      <span>继续追问</span>
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
  terminal,
  running,
  waitingForInput,
  beforeSteer,
  afterSteer,
  footer,
  onArtifactOpen,
  onArtifactDownload,
}: {
  messages: IAgentScopeRuntimeMessage[];
  timeline?: ChatTimelineItem[] | null;
  terminal: boolean;
  running: boolean;
  waitingForInput?: boolean;
  beforeSteer?: boolean;
  afterSteer?: boolean;
  footer?: ReactNode;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
}) {
  return (
    <article className={`${styles.runtimeResponseRow} ${beforeSteer ? styles.runtimeResponseRowBeforeSteer : ''} ${afterSteer ? styles.runtimeResponseRowAfterSteer : ''}`}>
      <div className={`${styles.runtimeMessageAvatar} ${styles.runtimeAgentAvatar}`} aria-hidden="true">
        <img src="/mascot-blue-transparent.png" alt="" />
      </div>
      <div className={styles.runtimeResponseColumn}>
        <div className={styles.runtimeResponseSurface}>
          <div className={styles.runtimeResponseContent}>
            <RuntimeProcessTimeline
              messages={messages}
              timeline={timeline}
              terminal={terminal}
              onArtifactOpen={onArtifactOpen}
              onArtifactDownload={onArtifactDownload}
            />
          </div>
          {running ? (
            <div className={styles.runtimeResponseLoading} role="status" aria-live="polite">
              <LoadingOutlined spin aria-hidden="true" />
              <span>
                {waitingForInput
                  ? '等待你的确认，回复尚未结束'
                  : '正在生成回复，请稍候…'}
              </span>
            </div>
          ) : null}
        </div>
        {footer}
      </div>
    </article>
  );
}

const RuntimeConversation = forwardRef<
  RuntimeConversationHandle,
  RuntimeConversationProps
>(function RuntimeConversation({
  controller,
  onArtifactOpen,
  onArtifactDownload,
  onOpenAgentConfiguration,
}, forwardedRef) {
  const rootRef = useRef<HTMLDivElement>(null);
  const runtimeRef = useRef<IAgentScopeRuntimeWebUIRef>(null);
  const syncedRuntimeSessionIdRef = useRef<string | null>(null);
  const runtimeSessionGeneratingRef = useRef(false);
  const scrollSequenceRef = useRef(0);
  const anchorHighlightTimerRef = useRef<number | null>(null);
  const [dataSource, setDataSource] = useState(controller.selectedDatasourceId ?? undefined);
  const [steeringItemId, setSteeringItemId] = useState<string | null>(null);
  const [dismissedClarificationKeys, setDismissedClarificationKeys] = useState(
    () => new Set<string>(),
  );
  const runtimeSession = controller.runtimeSession;
  const currentChatId = controller.currentChatId;
  const answerClarification = controller.answerClarification;
  const [controlledRuntimeSessionId, setControlledRuntimeSessionId] = useState(
    runtimeSession?.id,
  );
  const clarificationCandidate = controller.currentProjection?.chat.status === 'running'
    ? selectPendingAskUserQuestion(controller.currentProjection.messages)
    : null;
  const clarificationKey = clarificationCandidate && currentChatId
    ? `${currentChatId}:${clarificationCandidate.id}`
    : null;
  const clarificationDismissed = clarificationKey
    ? dismissedClarificationKeys.has(clarificationKey)
    : false;
  const clarificationExpired = clarificationCandidate
    ? isAskUserQuestionExpired(clarificationCandidate)
    : false;
  const pendingClarification = clarificationCandidate
    && !clarificationDismissed
    && !clarificationExpired
    ? clarificationCandidate
    : null;

  const resolveClarification = useCallback(async (
    clarificationId: string,
    result: Parameters<ConsoleChatController['answerClarification']>[2],
  ) => {
    const chatId = currentChatId;
    if (!chatId) throw new Error('当前 Chat 尚未加载完成');
    const key = `${chatId}:${clarificationId}`;
    setDismissedClarificationKeys((current) => {
      if (current.has(key)) return current;
      const next = new Set(current);
      next.add(key);
      return next;
    });
    await answerClarification(chatId, clarificationId, result);
  }, [answerClarification, currentChatId]);

  useEffect(() => {
    if (
      !clarificationCandidate
      || !clarificationKey
      || clarificationDismissed
      || !clarificationExpired
    ) {
      return;
    }
    void resolveClarification(
      clarificationCandidate.id,
      { status: 'timeout', reason: '等待用户输入超时' },
    ).catch(() => undefined);
  }, [
    clarificationCandidate,
    clarificationDismissed,
    clarificationExpired,
    clarificationKey,
    resolveClarification,
  ]);

  const scrollToChat = useCallback((chatId: string) => {
    const sequence = scrollSequenceRef.current + 1;
    scrollSequenceRef.current = sequence;

    const scroll = async () => {
      let scrollContainer: HTMLElement | null = null;
      let target: HTMLElement | null = null;

      for (let attempt = 0; attempt < 3 && !target; attempt += 1) {
        await waitForAnimationFrame();
        if (scrollSequenceRef.current !== sequence) return;

        scrollContainer = rootRef.current?.querySelector<HTMLElement>(
          runtimeScrollContainerSelector,
        ) ?? null;
        target = scrollContainer
          ? getMessageElementInScrollContainer(
              scrollContainer,
              `request-${chatId}`,
            )
          : null;
      }

      if (!scrollContainer || !target || scrollSequenceRef.current !== sequence) {
        return;
      }

      scrollTargetIntoContainerTop(
        scrollContainer,
        target,
        'smooth',
        getScrollVisibleTopOffset(scrollContainer),
      );

      if (anchorHighlightTimerRef.current !== null) {
        window.clearTimeout(anchorHighlightTimerRef.current);
      }
      target.classList.remove(runtimeAnchorActiveClass);
      window.requestAnimationFrame(() => {
        if (scrollSequenceRef.current !== sequence) return;
        target.classList.add(runtimeAnchorActiveClass);
        anchorHighlightTimerRef.current = window.setTimeout(() => {
          target.classList.remove(runtimeAnchorActiveClass);
          anchorHighlightTimerRef.current = null;
        }, runtimeAnchorActiveDuration);
      });
    };

    void scroll();
  }, []);

  useImperativeHandle(forwardedRef, () => ({ scrollToChat }), [scrollToChat]);

  useEffect(() => () => {
    scrollSequenceRef.current += 1;
    if (anchorHighlightTimerRef.current !== null) {
      window.clearTimeout(anchorHighlightTimerRef.current);
    }
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;

    let observedInput: HTMLElement | null = null;
    const updateInputHeight = () => {
      const inputHeight = observedInput?.getBoundingClientRect().height ?? 0;
      if (Number.isFinite(inputHeight) && inputHeight > 0) {
        root.style.setProperty(runtimeInputHeightProperty, `${Math.ceil(inputHeight)}px`);
      }
    };
    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(updateInputHeight);
    const syncObservedInput = () => {
      const nextInput = root.querySelector<HTMLElement>(runtimeInputSelector);
      if (nextInput !== observedInput) {
        if (observedInput) resizeObserver?.unobserve(observedInput);
        observedInput = nextInput;
        if (observedInput) resizeObserver?.observe(observedInput);
      }
      updateInputHeight();
    };
    const mutationObserver = new MutationObserver(syncObservedInput);

    mutationObserver.observe(root, { childList: true, subtree: true });
    syncObservedInput();

    return () => {
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
      root.style.removeProperty(runtimeInputHeightProperty);
    };
  }, []);

  useEffect(() => {
    setDataSource(controller.selectedDatasourceId ?? undefined);
  }, [controller.selectedDatasourceId]);

  useEffect(() => {
    setControlledRuntimeSessionId((currentSessionId) => (
      selectControlledRuntimeSessionId({
        currentSessionId,
        nextSessionId: runtimeSession?.id,
        connectionStatus: controller.connectionStatus,
      })
    ));
  }, [controller.connectionStatus, runtimeSession?.id]);

  useEffect(() => {
    const runtime = runtimeRef.current;
    if (!runtime || !controller.runtimeSession) return;

    const previousSessionId = syncedRuntimeSessionIdRef.current;
    const wasGenerating = runtimeSessionGeneratingRef.current;
    const isGenerating = Boolean(controller.runtimeSession.generating);
    runtimeSessionGeneratingRef.current = isGenerating;
    const syncMode = selectRuntimeMessageSyncMode({
      previousSessionId,
      nextSessionId: controller.runtimeSession.id,
      wasGenerating,
      isGenerating,
      connectionStatus: controller.connectionStatus,
    });
    syncedRuntimeSessionIdRef.current = controller.runtimeSession.id;

    // AgentScope's createSession flow owns the optimistic request while the
    // controlled route catches up from the local draft id to the persisted id.
    // Keep that pair mounted until the first response settles.
    if (syncMode === 'none') return;

    const frame = window.requestAnimationFrame(() => {
      runtime.messages.removeAllMessages();
      controller.runtimeSession?.messages.forEach((message) => {
        runtime.messages.updateMessage(message);
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [controller.connectionStatus, controller.runtimeSession]);

  useEffect(() => {
    runtimeRef.current?.input.setDisabled(controller.connectionStatus === 'loading');
  }, [controller.connectionStatus]);

  const sessionApi = useMemo<IAgentScopeRuntimeWebUISessionAPI>(() => ({
    getSessionList: async () => runtimeSession ? [runtimeSession] : [],
    getSession: async () => {
      if (!runtimeSession) throw new Error('会话尚未加载完成');
      return runtimeSession;
    },
    updateSession: async (patch) => (
      runtimeSession
        ? [{ ...runtimeSession, ...patch }]
        : []
    ),
    createSession: async () => runtimeSession ? [runtimeSession] : [],
    removeSession: async () => runtimeSession ? [runtimeSession] : [],
  }), [runtimeSession]);

  const handleKeyDownCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' && event.altKey) {
      // AgentScope's queue capture treats every non-Shift Enter as submit.
      // Keep Option/Alt+Enter available as the second documented newline gesture.
      event.stopPropagation();
    }
  };

  const queueItemActions = useMemo<readonly IAgentScopeRuntimeWebUIQueueItemAction[]>(
    () => [{
      key: 'steer-current-chat',
      label: ({ item }) => item.data.query?.trim()
        ? '作为 Steer 注入当前对话'
        : '仅包含附件的队列项不能作为 Steer',
      icon: ({ item }) => steeringItemId === item.id
        ? <LoadingOutlined spin />
        : <CompassOutlined />,
      visible: isRuntimeQueueSteerVisible(controller.currentProjection?.chat.status),
      ownerOnly: runtimeQueueSteerOwnerOnly,
      disabled: ({ item }) => (
        !item.data.query?.trim()
        || (steeringItemId !== null && steeringItemId !== item.id)
      ),
      onClick: async ({ item, remove }) => {
        const text = item.data.query?.trim();
        if (!text) {
          message.warning('仅包含附件的队列项不能作为引导');
          return;
        }
        setSteeringItemId(item.id);
        try {
          await controller.steerCurrentChat(text);
          remove();
          message.success('已将队列内容注入当前对话');
        } catch (error) {
          message.error(error instanceof Error ? error.message : '引导失败');
        } finally {
          setSteeringItemId(null);
        }
      },
    }],
    [controller, steeringItemId],
  );

  const runtimeInputQueueSessionId = resolveRuntimeInputQueueSessionId({
    runtimeSessionId: controlledRuntimeSessionId,
    persistedSessionId: controller.session?.id,
    draftQueueSessionId: controller.inputQueueSessionId,
  });
  const runtimeInputQueueEnabled = shouldUseRuntimeInputQueue(
    controller.currentProjection?.chat.status,
    hasInputQueueItems(runtimeInputQueueSessionId),
  );

  const options = useMemo<IAgentScopeRuntimeWebUIOptions>(() => ({
    theme: {
      ...runtimeThemeOptions,
      bubbleList: {
        pagination: false,
        userMessageAnchors: {
          enabled: false,
        },
      },
    },
    welcome: {
      nick: getRuntimeConfig().assistant_name,
      greeting: getRuntimeConfig().welcome_title,
      description: getRuntimeConfig().welcome_description,
      avatar: getRuntimeConfig().avatar_url,
    },
    sender: {
      placeholder: '继续补充分析要求；运行中输入将进入前端等待队列',
      maxLength: 4000,
      showCharacterCount: false,
      // The SDK branches on the presence of `queue` before it calls onSubmit.
      // Keep it absent for an idle draft so the first message can create the
      // backend Session; enable it once a Chat is running or work is pending.
      queue: runtimeInputQueueEnabled ? {
        enable: true,
        maxSize: 20,
        itemActions: queueItemActions,
        getSessionId: (runtimeSessionId) => resolveRuntimeInputQueueSessionId({
          runtimeSessionId,
          persistedSessionId: controller.session?.id,
          draftQueueSessionId: controller.inputQueueSessionId,
        }),
        getRequestContext: () => controller.session
          ? {
              session_id: controller.session.id,
              channel: 'main',
              agent_id: controller.session.agent_id,
            }
          : undefined,
        isSessionRunning: controller.isCurrentChatRunning,
        shouldRestoreOnError: () => true,
      } : false,
      prefix: (
        <Select
          className={styles.runtimeDatasourceSelect}
          classNames={{ popup: { root: styles.runtimeDatasourceDropdown } }}
          key="runtime-datasource"
          size="small"
          value={dataSource}
          placeholder="选择数据源"
          disabled={Boolean(controller.session?.datasource_id)}
          onChange={(value) => {
            setDataSource(value);
            controller.selectDatasource(value);
          }}
          options={controller.datasources.map((item) => ({
            value: item.id,
            label: `${item.name}${item.recommended ? ' · 推荐' : ''}`,
          }))}
        />
      ),
      actionAffix: ({ value }) => (
        <>
          <span className={styles.runtimeShortcut}>Enter 发送 · Shift/⌥ Enter 换行</span>
          {shouldShowRuntimeStopButton(
            controller.currentProjection?.chat.status,
            value,
          ) ? (
            <Tooltip title="停止生成">
              <Button
                aria-label="停止生成"
                className={styles.runtimeStopButton}
                data-runtime-stop-button="true"
                onClick={controller.stopCurrentChat}
                type="primary"
              >
                <span aria-hidden="true" className={styles.runtimeStopGlyph} />
              </Button>
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
      fetch: async ({ input, signal }) => {
        const text = extractLatestRuntimeText(input);
        if (!text) throw new Error('请输入分析问题');
        const response = await controller.submitRuntimeRequest(text, signal);
        clearRuntimeInputAfterAccepted(rootRef.current, text);
        return response;
      },
      responseParser: (chunk) => parseRuntimeStreamChunk(String(chunk)),
      cancel: controller.stopCurrentChat,
    },
    customToolRenderConfig: runtimeToolRenderConfig,
    request: {
      render: ({ fallback }) => (
        <article className={styles.runtimeRequestShell}>
          <div className={styles.runtimeRequestBody}>{fallback()}</div>
          <div className={`${styles.runtimeMessageAvatar} ${styles.runtimeUserAvatar}`} aria-hidden="true">
            <UserOutlined />
          </div>
        </article>
      ),
    },
    response: {
      render: ({ data }) => {
        const projection = controller.projections.find((item) => (
          item.responseId === data.id
          || `response-${item.chat.id}` === data.id
        ));
        const running = isRuntimeResponseRunning(projection?.chat.status, data.status);
        const chatId = projection?.chat.id;
        const timeline = projection ? selectChatTimeline(projection) : null;
        const sections: RuntimeResponseSection[] = timeline
          ? splitChatTimelineBySteer(timeline)
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
                return <RuntimeSteerMessage text={section.text} key={section.id} />;
              }
              const showFooter = index === lastResponseIndex && !running && Boolean(chatId);
              return (
                <RuntimeResponseMessage
                  key={section.id}
                  messages={data.output}
                  timeline={section.items}
                  terminal={projection ? projection.chat.status !== 'running' : !running}
                  running={running && index === lastResponseIndex}
                  waitingForInput={Boolean(
                    pendingClarification
                    && chatId === controller.currentChatId
                    && index === lastResponseIndex
                  )}
                  beforeSteer={sections[index + 1]?.kind === 'steer'}
                  afterSteer={sections[index - 1]?.kind === 'steer'}
                  onArtifactOpen={onArtifactOpen}
                  onArtifactDownload={onArtifactDownload}
                  footer={showFooter && chatId ? (
                    <>
                      <RuntimeResponseActions
                        chatId={chatId}
                        getText={() => serializeRuntimeResponseToMarkdown(data.output)}
                        onFeedback={(kind, detail) => controller.submitFeedback(chatId, kind, detail)}
                        loadReaction={controller.getFeedbackReaction}
                        clearReaction={controller.clearFeedbackReaction}
                      />
                      <RuntimeFollowupHints
                        questions={controller.projections.find(({ chat }) => chat.id === chatId)
                          ?.chat.followup?.questions ?? []}
                        onSelect={(question) => fillRuntimeInput(rootRef.current, question)}
                      />
                    </>
                  ) : null}
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
  }), [
    controlledRuntimeSessionId,
    controller,
    dataSource,
    onArtifactDownload,
    onArtifactOpen,
    pendingClarification,
    queueItemActions,
    runtimeInputQueueEnabled,
    sessionApi,
  ]);

  return (
    <div
      className={styles.runtimeConversation}
      data-has-clarification={pendingClarification ? 'true' : 'false'}
      data-runtime-webui="true"
      data-timeline-items={controller.currentTimeline.length}
      onKeyDownCapture={handleKeyDownCapture}
      ref={rootRef}
    >
      {controller.error ? (
        <div className={styles.runtimeConnectionNotice} role="alert">
          <span>{controller.error}</span>
          {controller.modelConfigurationRequired && onOpenAgentConfiguration ? (
            <Button size="small" type="link" onClick={onOpenAgentConfiguration}>
              去配置 Agent
            </Button>
          ) : null}
        </div>
      ) : null}
      {!controller.projections.length && getRuntimeConfig().sample_questions.length > 0 ? (
        <div className={styles.followupHints} aria-label="示例问题">
          <span>试着问问</span>
          {getRuntimeConfig().sample_questions.map((question) => (
            <button type="button" key={question} onClick={() => fillRuntimeInput(rootRef.current, question)}>
              {question}
            </button>
          ))}
        </div>
      ) : null}
      <AntConfigProvider prefixCls="data-runtime" theme={runtimeAntTheme}>
        {/* The SDK stores loading by Runtime Session ID and blocks queue draining
            during a route change. Remount for each draft and again when `new`
            becomes the persisted Session so loading and drain state are rebuilt
            from the host's authoritative snapshot. */}
        <AgentScopeRuntimeWebUI
          key={`${controller.inputQueueSessionId}:${controlledRuntimeSessionId ?? 'new'}`}
          ref={runtimeRef}
          options={options}
        />
      </AntConfigProvider>
      {pendingClarification && controller.currentChatId ? (
        <RuntimeClarificationCard
          key={pendingClarification.id}
          clarification={pendingClarification}
          onSubmit={(answers) => resolveClarification(
            pendingClarification.id,
            { status: 'answered', answers },
          )}
          onTimeout={() => resolveClarification(
            pendingClarification.id,
            { status: 'timeout', reason: '等待用户输入超时' },
          )}
        />
      ) : null}
    </div>
  );
});

export default RuntimeConversation;
