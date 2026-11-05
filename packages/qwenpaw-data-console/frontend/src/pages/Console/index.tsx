import { getRuntimeConfig } from '@/services/runtimeConfig';
import {
  FileTextOutlined,
  LinkOutlined,
  MenuOutlined,
  DownloadOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import { Button, Modal } from '@agentscope-ai/design';
import { LiquidGlass } from '@liquidglass/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { dataConsoleApi } from '@/features/console/api/client';
import ArtifactPreview from '@/features/console/components/ArtifactPreview';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import ConsoleSettingsMenu from '@/features/console/components/ConsoleSettingsMenu';
import OverflowTooltip from '@/features/console/components/OverflowTooltip';
import RuntimeConversation from '@/features/console/components/RuntimeConversation';
import type { RuntimeConversationHandle } from '@/features/console/components/RuntimeConversation';
import SessionWorkspace from '@/features/console/components/SessionWorkspace';
import { accessArtifact } from '@/features/console/model/artifacts';
import { selectPendingAskUserQuestion } from '@/features/console/model/askUserQuestion';
import { buildAgentConfigurationPath } from '@/features/console/model/sessionNavigation';
import type { Artifact, ConversationTurn, WorkspaceTab } from '@/features/console/types';
import styles from '@/features/console/index.module.less';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';

const DEFAULT_WORKSPACE_WIDTH = 440;
const MIN_WORKSPACE_WIDTH = 340;
const WORKSPACE_RESIZE_STEP = 16;

interface WorkspaceResizeState {
  pointerId: number;
  startX: number;
  startWidth: number;
  currentWidth: number;
}

function ConsoleHeader({
  title,
  channel,
  onOpenNav,
}: {
  title: string;
  channel?: string;
  onOpenNav: () => void;
}) {
  return (
    <LiquidGlass
      borderRadius={0}
      blur={10}
      contrast={1.08}
      brightness={1.06}
      saturation={1.24}
      shadowIntensity={0}
      displacementScale={0.42}
      elasticity={0.38}
      zIndex={4}
      className={styles.consoleHeaderGlass}
    >
      <header className={styles.consoleHeader}>
        <div className={styles.headerLeft}>
          <Button
            className={styles.mobileMenu}
            type="text"
            icon={<MenuOutlined />}
            aria-label="打开导航"
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
        </div>
        <div className={styles.headerActions}>
          <ConsoleSettingsMenu />
        </div>
      </header>
    </LiquidGlass>
  );
}

export default function ConsolePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { consoleController, openMobileNav } = useConsoleLayout();
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('turns');
  const [settlementRefreshKey, setSettlementRefreshKey] = useState(0);
  const [previewArtifact, setPreviewArtifact] = useState<Artifact | null>(null);
  const [workspaceWidth, setWorkspaceWidth] = useState(DEFAULT_WORKSPACE_WIDTH);
  const [workspaceMaxWidth, setWorkspaceMaxWidth] = useState(DEFAULT_WORKSPACE_WIDTH);
  const [isWorkspaceCollapsed, setIsWorkspaceCollapsed] = useState(
    () => !new URLSearchParams(window.location.search).get('session_id'),
  );
  const [isWorkspaceResizing, setIsWorkspaceResizing] = useState(false);
  const pageRef = useRef<HTMLDivElement>(null);
  const runtimeConversationRef = useRef<RuntimeConversationHandle>(null);
  const workspaceResizeRef = useRef<WorkspaceResizeState | null>(null);
  const workspaceResizeCleanupRef = useRef<(() => void) | null>(null);
  const handleArtifactAccess = useCallback(async (
    artifact: Artifact,
    purpose: 'preview' | 'download',
  ) => {
    const sessionId = consoleController.session?.id;
    if (!sessionId) return;
    try {
      await accessArtifact(sessionId, artifact, purpose, dataConsoleApi);
    } catch (artifactError) {
      const message = artifactError instanceof Error
        ? artifactError.message
        : String(artifactError);
      window.alert(`文件访问失败：${message}`);
    }
  }, [consoleController.session?.id]);

  const handleArtifactDownload = useCallback((artifact: Artifact) => {
    void handleArtifactAccess(artifact, 'download');
  }, [handleArtifactAccess]);
  const turns = useMemo<ConversationTurn[]>(() => (
    consoleController.projections
      .map((projection) => {
        const { chat } = projection;
        const datasourceName = consoleController.datasources
          .find((datasource) => datasource.id === chat.datasource_id)?.name
          ?? chat.datasource_id
          ?? '未选择数据源';
        const sentAtDate = chat.started_at ? new Date(chat.started_at) : null;
        const sentAt = sentAtDate && !Number.isNaN(sentAtDate.getTime())
          ? sentAtDate.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
          : '';

        const status: ConversationTurn['status'] = chat.status === 'running'
          ? selectPendingAskUserQuestion(projection.messages)
            ? 'awaiting'
            : 'running'
          : 'done';

        return {
          id: chat.id,
          number: String(chat.sequence).padStart(2, '0'),
          title: chat.user_input || `第 ${chat.sequence} 轮对话`,
          summary: '',
          dataSource: datasourceName,
          sentAt,
          status,
        };
      })
      .sort((left, right) => Number(left.number) - Number(right.number))
  ), [consoleController.datasources, consoleController.projections]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = getRuntimeConfig().title;

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
    const chatJustFinished = previousChatState.chatId === currentChatId
      && previousChatState.status === 'running' && (
      currentChatStatus === 'completed'
      || currentChatStatus === 'failed'
      || currentChatStatus === 'canceled'
    );
    if (!chatJustFinished) return undefined;

    const timeoutId = window.setTimeout(() => {
      setSettlementRefreshKey((current) => current + 1);
    }, 1_500);
    return () => window.clearTimeout(timeoutId);
  }, [currentChatId, currentChatStatus]);

  useEffect(() => () => {
    workspaceResizeCleanupRef.current?.();
  }, []);

  useEffect(() => {
    const syncWorkspaceBounds = () => {
      const page = pageRef.current;
      const workspace = page?.querySelector<HTMLElement>(`.${styles.sessionWorkspace}`);

      if (!page || !workspace || workspace.offsetWidth === 0) {
        return;
      }

      const availableWidth = page.getBoundingClientRect().width;
      const maxWidth = Math.max(MIN_WORKSPACE_WIDTH, Math.floor(availableWidth / 2));
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

  const handleTurnSelect = (turn: ConversationTurn) => {
    consoleController.selectChat(turn.id);
    runtimeConversationRef.current?.scrollToChat(turn.id);
  };

  const getWorkspaceBounds = () => {
    const page = pageRef.current;

    if (!page) {
      return null;
    }

    const availableWidth = page.getBoundingClientRect().width;

    return {
      min: MIN_WORKSPACE_WIDTH,
      max: Math.max(MIN_WORKSPACE_WIDTH, Math.floor(availableWidth / 2)),
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

    const workspace = pageRef.current?.querySelector<HTMLElement>(`.${styles.sessionWorkspace}`);

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
    const workspace = pageRef.current?.querySelector<HTMLElement>(`.${styles.sessionWorkspace}`);

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

  return (
    <div
      className={`${styles.consolePage} ${
        isWorkspaceResizing ? styles.workspaceResizing : ''
      } ${isWorkspaceCollapsed ? styles.workspaceCollapsed : ''}`}
      ref={pageRef}
    >
      <section className={styles.console}>
        <ConsoleHeader
          title={consoleController.session?.title || 'Data Console'}
          channel={consoleController.session?.channel}
          onOpenNav={openMobileNav}
        />
        <RuntimeConversation
          controller={consoleController}
          onArtifactOpen={setPreviewArtifact}
          onArtifactDownload={handleArtifactDownload}
          onOpenAgentConfiguration={() => navigate(buildAgentConfigurationPath())}
          ref={runtimeConversationRef}
        />
      </section>

      <SessionWorkspace
        activeTab={workspaceTab}
        selectedTurnId={consoleController.currentChatId ?? ''}
        turns={turns}
        chatProjection={consoleController.currentProjection}
        sessionId={consoleController.session?.id}
        settlementRefreshKey={settlementRefreshKey}
        onTabChange={setWorkspaceTab}
        onRevealSettlementCards={handleRevealSettlementCards}
        onTurnSelect={handleTurnSelect}
        onPlanEdit={consoleController.editPlan}
        onArtifactOpen={setPreviewArtifact}
        onArtifactDownload={handleArtifactDownload}
        onLoadRawTrace={consoleController.loadRawTrace}
      />
      <div
        className={styles.workspaceResizeHandle}
        role="separator"
        tabIndex={0}
        aria-controls="session-workspace"
        aria-label="调整会话空间宽度"
        aria-orientation="vertical"
        aria-valuemin={MIN_WORKSPACE_WIDTH}
        aria-valuemax={workspaceMaxWidth}
        aria-valuenow={workspaceWidth}
        aria-valuetext={`${workspaceWidth} 像素`}
        title="拖动调整会话空间宽度"
        onKeyDown={handleWorkspaceResizeKeyDown}
        onPointerDown={handleWorkspaceResizeStart}
      />
      <button
        className={styles.workspaceCollapseHandle}
        type="button"
        aria-controls="session-workspace"
        aria-expanded={!isWorkspaceCollapsed}
        aria-label={isWorkspaceCollapsed ? '展开会话空间' : '向右收起会话空间'}
        title={isWorkspaceCollapsed ? '展开会话空间' : '向右收起会话空间'}
        onClick={handleWorkspaceCollapseToggle}
      >
        {isWorkspaceCollapsed ? <LeftOutlined /> : <RightOutlined />}
      </button>

      <Modal
        className={styles.artifactModal}
        width={960}
        open={Boolean(previewArtifact)}
        title={(
          <div className={styles.artifactModalTitle}>
            <div>
              <strong title={previewArtifact?.name}>{previewArtifact?.name}</strong>
              {previewArtifact?.title && previewArtifact.title !== previewArtifact.name ? (
                <span title={previewArtifact.title}>{previewArtifact.title}</span>
              ) : null}
            </div>
          </div>
        )}
        footer={[
          <Button key="comment" icon={<FileTextOutlined />} disabled>
            选中行并评论
          </Button>,
          <Button
            key="download"
            icon={<DownloadOutlined />}
            onClick={() => {
              if (previewArtifact) void handleArtifactAccess(previewArtifact, 'download');
            }}
          >
            下载
          </Button>,
          <Button
            key="open"
            type="primary"
            icon={<LinkOutlined />}
            onClick={() => {
              if (previewArtifact) void handleArtifactAccess(previewArtifact, 'preview');
            }}
          >
            新窗口打开
          </Button>,
        ]}
        onCancel={() => setPreviewArtifact(null)}
      >
        {previewArtifact && consoleController.session?.id ? (
          <div className={styles.artifactPreviewContent}>
            <ArtifactPreview
              artifact={previewArtifact}
              sessionId={consoleController.session.id}
            />
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
