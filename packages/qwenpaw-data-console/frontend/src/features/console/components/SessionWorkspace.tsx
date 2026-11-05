import {
  BranchesOutlined,
  BulbOutlined,
  CheckCircleFilled,
  CheckOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  CodeOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  LoadingOutlined,
  MessageOutlined,
  PlusOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import {
  Button,
  Collapse,
  Input,
  Modal,
  Progress,
  Select,
  Tag,
  Tooltip,
} from '@agentscope-ai/design';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { Empty, Segmented, message, notification } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type {
  Artifact as ApiArtifact,
  Plan,
  PlanTask,
  SegmentArtifact as ApiSegmentArtifact,
} from '../api/types';
import type { SettlementCard } from '../api/settlement';
import {
  appendPlanTask,
  buildPlanSubmission,
  createPlanDraft,
  removePlanTask,
  updatePlanTaskSubject,
} from '../model/planEditor';
import { buildCronJobName, buildCronMessage } from '../model/cronMessage';
import type { ChatStreamProjection } from '../model/projectStream';
import { serializeTraceContentToMarkdown } from '../model/markdownContent';
import { selectRawTraceItems, type RawTraceItem } from '../model/rawTrace';
import { getSettlementCardTitle } from '../model/settlementCard';
import { formatSegmentMarkdown } from '../model/segmentMarkdown';
import { selectChatTimeline } from '../model/selectChatTimeline';
import { buildCronJobsPath } from '../model/sessionNavigation';
import { useSettlementCards } from '../model/useSettlementCards';
import type { Artifact, ConversationTurn, WorkspaceTab } from '../types';
import ArtifactCard from './ArtifactCard';
import OverflowTooltip from './OverflowTooltip';
import SettlementPanel from './SettlementPanel';
import styles from '../index.module.less';

interface SessionWorkspaceProps {
  activeTab: WorkspaceTab;
  selectedTurnId: string;
  turns: ConversationTurn[];
  chatProjection: ChatStreamProjection | null;
  sessionId?: string | null;
  settlementRefreshKey?: number;
  onTabChange: (tab: WorkspaceTab) => void;
  onRevealSettlementCards: () => void;
  onTurnSelect: (turn: ConversationTurn) => void;
  onPlanEdit: (chatId: string, plan: Plan, reason?: string | null) => Promise<void>;
  onArtifactOpen: (artifact: Artifact) => void;
  onArtifactDownload: (artifact: Artifact) => void;
  onLoadRawTrace?: (chatId: string) => Promise<void>;
}

interface TurnSelectorProps {
  selectedTurnId: string;
  turns: ConversationTurn[];
  onTurnSelect: (turn: ConversationTurn) => void;
}

interface WorkspaceEmptyProps {
  title: string;
  description: string;
}

interface PlanEditorModalProps {
  chatLabel: string;
  chatStatus: 'created' | 'running' | 'completed' | 'failed' | 'canceled';
  plan: Plan;
  onCancel: () => void;
  onSubmit: (plan: Plan) => Promise<void>;
}

const planStateCopy: Record<PlanTask['state'], { label: string; color?: string }> = {
  completed: {
    label: '已完成',
    color: 'success',
  },
  in_progress: {
    label: '运行中',
    color: 'processing',
  },
  pending: {
    label: '待执行',
  },
};

const SETTLEMENT_NOTIFICATION_KEY = 'new-settlement-cards';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function PlanEditorModal({
  chatLabel,
  chatStatus,
  plan,
  onCancel,
  onSubmit,
}: PlanEditorModalProps) {
  const [draft, setDraft] = useState(() => createPlanDraft(plan));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canSubmit = draft.length > 0 && draft.every((task) => Boolean(task.subject.trim()));

  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(buildPlanSubmission(draft));
    } catch (submitError) {
      setError(errorMessage(submitError));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      className={styles.planEditorModal}
      width={680}
      title="修改执行计划"
      open
      okText="提交计划"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{ disabled: !canSubmit }}
      onOk={() => void submit()}
      onCancel={() => {
        if (!submitting) onCancel();
      }}
    >
      <OverflowTooltip title={chatLabel}>
        <div className={styles.planEditorChat}>{chatLabel}</div>
      </OverflowTooltip>
      <div className={styles.planEditorNotice}>
        {chatStatus === 'running'
          ? '提交后，Agent 会从当前状态重新评估后续步骤；已经完成的执行轨迹仍会保留。'
          : '这是已结束 Chat 的计划修订，不会覆盖原始 Trace；后续重新执行时将采用新计划。'}
      </div>
      <div className={styles.planEditorStepCount}>{draft.length} 个计划步骤</div>
      <div className={styles.planEditorList}>
        {draft.map((task, index) => {
          const copy = planStateCopy[task.state];
          return (
            <div className={styles.planEditorTask} key={task.id}>
              <span className={styles.planEditorTaskIndex} aria-hidden="true">
                Step {String(index + 1).padStart(2, '0')}
              </span>
              <div className={styles.planEditorTaskRow}>
                <label className={styles.planEditorTaskField}>
                  <Input
                    maxLength={120}
                    value={task.subject}
                    placeholder="输入计划步骤标题"
                    aria-label={`计划步骤 ${index + 1} 标题`}
                    suffix={(
                      <Tag bordered={false} color={copy.color} size="small">{copy.label}</Tag>
                    )}
                    onChange={(event) => setDraft((current) => (
                      updatePlanTaskSubject(current, task.id, event.target.value)
                    ))}
                  />
                </label>
                <Tooltip title={`删除计划步骤 ${index + 1}`}>
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    aria-label={`删除计划步骤 ${index + 1}`}
                    onClick={() => setDraft((current) => removePlanTask(current, task.id))}
                  />
                </Tooltip>
              </div>
            </div>
          );
        })}
      </div>
      <Button
        block
        className={styles.planEditorAdd}
        type="dashed"
        icon={<PlusOutlined />}
        onClick={() => setDraft((current) => appendPlanTask(current))}
      >
        新增计划步骤
      </Button>
      {error ? <div className={styles.planEditorError} role="alert">{error}</div> : null}
    </Modal>
  );
}

function WorkspaceEmpty({ title, description }: WorkspaceEmptyProps) {
  return (
    <Empty
      className={styles.workspaceEmptyState}
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={(
        <span className={styles.workspaceEmptyDescription}>
          <strong>{title}</strong>
          <span>{description}</span>
        </span>
      )}
    />
  );
}

function formatRawTraceBody(item: RawTraceItem): string {
  const input = serializeTraceContentToMarkdown(item.input.content) || '_无内容_';
  if (item.output === null) return input;
  const output = serializeTraceContentToMarkdown(item.output.content) || '_无内容_';
  return `### Input\n\n${input}\n\n### Output\n\n${output}`;
}

function RawTraceTypeIcon({ type }: { type: string }) {
  if (type === 'reasoning') {
    return (
      <span className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconReasoning}`} aria-hidden="true">
        <BulbOutlined />
      </span>
    );
  }

  if (type === 'message') {
    return (
      <span className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconMessage}`} aria-hidden="true">
        <MessageOutlined />
      </span>
    );
  }

  return (
    <span className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconTool}`} aria-hidden="true">
      <ToolOutlined />
    </span>
  );
}

function toDisplayArtifact(artifact: ApiArtifact): Artifact {
  const extension = artifact.name.split('.').pop()?.toUpperCase() || 'FILE';
  return {
    id: artifact.id,
    name: artifact.name,
    title: artifact.name,
    type: extension,
    meta: artifact.path,
    createdAt: artifact.created_at,
    path: artifact.path,
    access: {
      relativePath: artifact.path,
    },
  };
}

function toDisplaySegmentArtifact(
  segmentId: string,
  artifact: ApiSegmentArtifact,
): Artifact {
  const extension = artifact.name.split('.').pop()?.toUpperCase() || 'FILE';
  return {
    id: `${segmentId}:${artifact.relative_path}`,
    name: artifact.name,
    title: artifact.description,
    type: extension,
    meta: artifact.relative_path,
    important: true,
    path: artifact.relative_path,
    access: {
      relativePath: artifact.relative_path,
    },
  };
}

function TurnSelector({ selectedTurnId, turns, onTurnSelect }: TurnSelectorProps) {
  return (
    <section className={styles.turnSelector} aria-label="选择对话轮次">
      <div className={styles.turnSelectorHeader}>
        <div><strong>对话轮次</strong></div>
        <span>{turns.length} 轮</span>
      </div>

      <div className={styles.turnList}>
        {turns.map((turn) => (
          <button
            className={`${styles.turnItem} ${
              selectedTurnId === turn.id ? styles.turnItemActive : ''
            }`}
            key={turn.id}
            type="button"
            onClick={() => onTurnSelect(turn)}
          >
            <span className={styles.turnNumber} aria-hidden="true">
              Chat {turn.number}
            </span>
            <OverflowTooltip title={turn.title}>
              <strong className={styles.turnTitle}>{turn.title}</strong>
            </OverflowTooltip>
            <span className={styles.turnMeta}>
              <Tag
                bordered={false}
                color="blue"
                size="small"
                className={styles.turnDatasourceTag}
              >
                {turn.dataSource}
              </Tag>
              <time>{turn.sentAt}</time>
            </span>
          </button>
        ))}
        {!turns.length ? (
          <WorkspaceEmpty
            title="尚无对话轮次"
            description="从左侧输入问题后，这里会同步显示本次会话。"
          />
        ) : null}
      </div>
    </section>
  );
}

function TurnSelectDropdown({ selectedTurnId, turns, onTurnSelect }: TurnSelectorProps) {
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId) ?? turns[0];
  const options = useMemo(() => turns.map((turn) => ({
    value: turn.id,
    label: (
      <span className={styles.turnSelectOption}>
        <span className={styles.turnSelectOptionMeta}>
          <span>Chat {turn.number}</span>
          <span>{[turn.dataSource, turn.sentAt].filter(Boolean).join(' · ')}</span>
        </span>
        <span className={styles.turnSelectOptionTitle} title={turn.title}>
          {turn.title}
        </span>
      </span>
    ),
  })), [turns]);

  return (
    <Select
      className={styles.turnSelectControl}
      popupClassName={styles.turnSelectPopup}
      aria-label="选择对话轮次"
      value={selectedTurn?.id}
      placeholder="暂无对话轮次"
      disabled={!turns.length}
      options={options}
      onChange={(turnId) => {
        const turn = turns.find((item) => item.id === turnId);
        if (turn) onTurnSelect(turn);
      }}
    />
  );
}

function TasksPanel({
  selectedTurnId,
  turns,
  chatProjection,
  onTurnSelect,
  onPlanEdit,
}: Pick<
  SessionWorkspaceProps,
  'selectedTurnId' | 'turns' | 'chatProjection' | 'onTurnSelect' | 'onPlanEdit'
>) {
  const navigate = useNavigate();
  const [editingPlan, setEditingPlan] = useState(false);
  const taskTurn = turns.find((turn) => turn.id === selectedTurnId) ?? turns[0];
  const plan = chatProjection?.chat.plan;
  const planSteps = plan?.tasks ?? [];
  const completedCount = planSteps.filter((step) => step.state === 'completed').length;
  const progress = planSteps.length
    ? Math.round((completedCount / planSteps.length) * 100)
    : 0;
  const planTitle = planSteps.find((step) => step.state !== 'completed')?.subject
    ?? planSteps[0]?.subject
    ?? '执行计划';

  const handleRefineCron = () => {
    if (!chatProjection) return;
    const datasourceId = chatProjection.chat.datasource_id?.trim();
    if (!datasourceId) {
      message.error('请先选择数据源，再提炼为定时任务');
      return;
    }
    const query = chatProjection.chat.user_input || taskTurn?.title || '';
    navigate(buildCronJobsPath(), {
      state: {
        draft: {
          name: buildCronJobName(query),
          message: buildCronMessage(
            query,
            planSteps.map((step) => step.subject),
          ),
          datasource_id: datasourceId,
        },
      },
    });
  };

  return (
    <div className={styles.workspacePanel}>
      <TurnSelector
        selectedTurnId={selectedTurnId}
        turns={turns}
        onTurnSelect={onTurnSelect}
      />

      <section className={styles.workspaceContentSection}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>当前任务</strong>
            <OverflowTooltip title={taskTurn ? `Chat ${taskTurn.number} · ${taskTurn.title}` : '尚未开始'}>
              <span>{taskTurn ? `Chat ${taskTurn.number} · ${taskTurn.title}` : '尚未开始'}</span>
            </OverflowTooltip>
          </div>
        </div>

        {planSteps.length > 0 ? (
          <div className={styles.planPanel}>
            <div className={styles.planHeader}>
              <div>
                <span className={styles.planKicker}>
                  {chatProjection?.chat.kind === 'planned' ? '复杂计划' : '执行计划'}
                </span>
                <OverflowTooltip title={planTitle}>
                  <strong>{planTitle}</strong>
                </OverflowTooltip>
              </div>
              <Tooltip title="修改执行计划">
                <Button
                  type="text"
                  size="small"
                  icon={<EditOutlined />}
                  aria-label="修改计划"
                  onClick={() => setEditingPlan(true)}
                />
              </Tooltip>
            </div>

            <div className={styles.planProgress}>
              <Progress
                percent={progress}
                showInfo={false}
                strokeColor="var(--ant-color-primary)"
              />
              <span>{completedCount}/{planSteps.length} 已完成</span>
            </div>

            <div className={styles.planSteps}>
              {planSteps.map((step, index) => {
                const status = step.state === 'completed'
                  ? 'done'
                  : step.state === 'in_progress'
                    ? 'running'
                    : 'todo';
                return (
                  <div
                    className={`${styles.planStep} ${styles[`planStep_${status}`]}`}
                    key={step.id}
                  >
                    <span className={styles.planStepIndex}>{index + 1}</span>
                    <OverflowTooltip title={step.subject}>
                      <span className={styles.planStepTitle}>{step.subject}</span>
                    </OverflowTooltip>
                    {status === 'done' ? <CheckCircleFilled /> : null}
                    {status === 'running' ? <LoadingOutlined spin /> : null}
                  </div>
                );
              })}
            </div>

            <Button
              type="primary"
              className={styles.cronButton}
              icon={<ClockCircleOutlined />}
              onClick={handleRefineCron}
            >
              提炼为定时任务
            </Button>
          </div>
        ) : (
          <WorkspaceEmpty
            title="当前 Chat 暂无执行计划"
            description="简单问答不会生成计划；复杂任务开始规划后会实时显示。"
          />
        )}
        {editingPlan && plan && chatProjection ? (
          <PlanEditorModal
            key={chatProjection.chat.id}
            chatLabel={taskTurn ? `Chat ${taskTurn.number} · ${taskTurn.title}` : '当前 Chat'}
            chatStatus={chatProjection.chat.status}
            plan={plan}
            onCancel={() => setEditingPlan(false)}
            onSubmit={async (nextPlan) => {
              await onPlanEdit(
                chatProjection.chat.id,
                nextPlan,
                '用户在会话空间修改执行计划',
              );
              setEditingPlan(false);
            }}
          />
        ) : null}
      </section>
    </div>
  );
}

function isSkippedAgentArtifact(name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return ext === 'tif' || ext === 'tiff';
}

function ArtifactsPanel({
  turns,
  selectedTurnId,
  chatProjection,
  onTurnSelect,
  onArtifactOpen,
  onArtifactDownload,
}: Pick<SessionWorkspaceProps,
  | 'turns'
  | 'selectedTurnId'
  | 'chatProjection'
  | 'onTurnSelect'
  | 'onArtifactOpen'
  | 'onArtifactDownload'
>) {
  const [view, setView] = useState<'business' | 'agent'>('business');
  const [visibleCount, setVisibleCount] = useState(0);
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId);
  const artifacts = useMemo(() => {
    const seen = new Set<string>();
    if (view === 'agent') {
      return (chatProjection?.chat.artifacts ?? []).flatMap((artifact) => {
        if (isSkippedAgentArtifact(artifact.name) || seen.has(artifact.path)) return [];
        seen.add(artifact.path);
        return [toDisplayArtifact(artifact)];
      });
    }
    return (chatProjection?.chat.segments ?? []).flatMap((segment) => (
      (segment.artifact ?? []).flatMap((artifact) => {
        if (seen.has(artifact.relative_path)) return [];
        seen.add(artifact.relative_path);
        return [toDisplaySegmentArtifact(segment.segment_id, artifact)];
      })
    ));
  }, [chatProjection?.chat.artifacts, chatProjection?.chat.segments, view]);

  useEffect(() => {
    setVisibleCount(0);
    if (!artifacts.length) return undefined;

    let cancelled = false;
    let shown = 0;
    const chunkSize = 6;
    const reveal = () => {
      if (cancelled) return;
      shown = Math.min(artifacts.length, shown + chunkSize);
      setVisibleCount(shown);
      if (shown < artifacts.length) {
        window.requestAnimationFrame(reveal);
      }
    };
    const frame = window.requestAnimationFrame(reveal);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
    };
  }, [artifacts, view]);

  const visibleArtifacts = artifacts.slice(0, visibleCount);
  const listLoading = visibleCount < artifacts.length;

  return (
    <div className={`${styles.workspacePanel} ${styles.workspaceArtifactsPanel}`}>
      <TurnSelectDropdown
        selectedTurnId={selectedTurnId}
        turns={turns}
        onTurnSelect={onTurnSelect}
      />

      <section className={styles.workspaceContentSection}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>分析成果</strong>
            <span>{selectedTurn ? `Chat ${selectedTurn.number}` : '尚未选择 Chat'}</span>
          </div>
        </div>
        <Segmented
          block
          className={styles.workspaceModeSegmented}
          value={view}
          options={[
            { label: '业务视图', value: 'business' },
            { label: 'Agent 视图', value: 'agent' },
          ]}
          onChange={(value) => {
            setView(value as 'business' | 'agent');
            setVisibleCount(0);
          }}
        />

        {artifacts.length || listLoading ? (
          <div className={styles.artifactList}>
            {visibleArtifacts.map((artifact) => (
              <ArtifactCard
                artifact={artifact}
                key={artifact.id}
                showMoreMenu={view !== 'agent'}
                onPreview={() => onArtifactOpen(artifact)}
                onDownload={() => onArtifactDownload(artifact)}
              />
            ))}
            {listLoading ? (
              <div className={styles.artifactListLoading} role="status">
                <LoadingOutlined spin />
                <span>正在加载成果列表… {visibleCount}/{artifacts.length}</span>
              </div>
            ) : null}
          </div>
        ) : (
          <WorkspaceEmpty
            title="当前轮次暂无成果"
            description="后端注册 SQL、数据文件或报告后会实时出现在这里。"
          />
        )}
      </section>
    </div>
  );
}

function TracePanel({
  turns,
  selectedTurnId,
  chatProjection,
  onTurnSelect,
  onLoadRawTrace,
}: Pick<
  SessionWorkspaceProps,
  'turns' | 'selectedTurnId' | 'chatProjection' | 'onTurnSelect' | 'onLoadRawTrace'
>) {
  const [view, setView] = useState<'business' | 'agent'>('business');
  const [expandedRawTraceKeys, setExpandedRawTraceKeys] = useState<string[]>([]);
  const [expandedBusinessTraceState, setExpandedBusinessTraceState] = useState<{
    chatId: string | null;
    keys: string[];
  }>({ chatId: null, keys: [] });
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId);
  const businessTimeline = useMemo(() => (
    chatProjection
      ? selectChatTimeline(chatProjection, { keepCoveredBizEvents: true }).filter((item) => (
          item.kind === 'biz_event' || item.kind === 'segment'
        ))
      : []
  ), [chatProjection]);
  const rawMessages = useMemo(
    () => chatProjection?.messages ?? [],
    [chatProjection?.messages],
  );
  const rawTraceItems = useMemo(
    () => selectRawTraceItems(rawMessages),
    [rawMessages],
  );
  const rawMessageKeys = useMemo(
    () => rawTraceItems.map((item) => item.id),
    [rawTraceItems],
  );
  const rawMessageKeySet = useMemo(() => new Set(rawMessageKeys), [rawMessageKeys]);
  const activeRawTraceKeys = expandedRawTraceKeys.filter((key) => rawMessageKeySet.has(key));
  const activeRawTraceKeySet = useMemo(
    () => new Set(activeRawTraceKeys),
    [activeRawTraceKeys],
  );
  const businessTraceKeys = useMemo(
    () => businessTimeline.map((item) => item.id),
    [businessTimeline],
  );
  const businessTraceKeySet = useMemo(
    () => new Set(businessTraceKeys),
    [businessTraceKeys],
  );
  const currentChatId = chatProjection?.chat.id ?? null;
  const activeBusinessTraceKeys = useMemo(() => (
    expandedBusinessTraceState.chatId === currentChatId
      ? expandedBusinessTraceState.keys.filter((key) => businessTraceKeySet.has(key))
      : []
  ), [businessTraceKeySet, currentChatId, expandedBusinessTraceState]);
  const activeBusinessTraceKeySet = useMemo(
    () => new Set(activeBusinessTraceKeys),
    [activeBusinessTraceKeys],
  );
  const currentTraceCount = view === 'business' ? businessTimeline.length : rawTraceItems.length;

  useEffect(() => {
    if (view !== 'agent' || !selectedTurnId || !onLoadRawTrace) return;
    void onLoadRawTrace(selectedTurnId);
  }, [onLoadRawTrace, selectedTurnId, view]);

  return (
    <div className={`${styles.workspacePanel} ${styles.workspacePanelCompact}`}>
      <TurnSelectDropdown
        selectedTurnId={selectedTurnId}
        turns={turns}
        onTurnSelect={onTurnSelect}
      />

      <section className={`${styles.workspaceContentSection} ${styles.traceContentSection}`}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>执行轨迹</strong>
            <OverflowTooltip title={selectedTurn?.title || '尚未选择 Chat'}>
              <span className={styles.workspaceSelectedTurnTitle}>
                {selectedTurn?.title || '尚未选择 Chat'}
              </span>
            </OverflowTooltip>
          </div>
        </div>
        <Segmented
          block
          className={`${styles.workspaceModeSegmented} ${styles.traceModeSegmented}`}
          value={view}
          options={[
            { label: '业务轨迹', value: 'business' },
            { label: 'Agent 原始轨迹', value: 'agent' },
          ]}
          onChange={(value) => setView(value as 'business' | 'agent')}
        />

        <div className={styles.rawTraceActions}>
          <Button
            type="link"
            size="small"
            disabled={!currentTraceCount}
            onClick={() => {
              if (view === 'business') {
                setExpandedBusinessTraceState({
                  chatId: currentChatId,
                  keys: businessTraceKeys,
                });
              } else {
                setExpandedRawTraceKeys(rawMessageKeys);
              }
            }}
          >
            展开全部
          </Button>
          {(view === 'business'
            ? activeBusinessTraceKeys.length > 1
            : activeRawTraceKeys.length > 1) ? (
            <Button
              type="link"
              size="small"
              onClick={() => {
                if (view === 'business') {
                  setExpandedBusinessTraceState({ chatId: currentChatId, keys: [] });
                } else {
                  setExpandedRawTraceKeys([]);
                }
              }}
            >
              收起全部
            </Button>
          ) : null}
        </div>

        {view === 'business' && businessTimeline.length ? (
          <div className={styles.rawTraceContainer}>
            <Collapse
              className={styles.rawTraceCollapse}
              bordered={false}
              destroyOnHidden
              activeKey={activeBusinessTraceKeys}
              onChange={(keys) => {
                setExpandedBusinessTraceState({
                  chatId: currentChatId,
                  keys: (Array.isArray(keys) ? keys : [keys]).map(String),
                });
              }}
              items={businessTimeline.map((item, index) => {
                const isBusinessEvent = item.kind === 'biz_event';
                const isError = isBusinessEvent && item.event.status === 'error';
                const title = isBusinessEvent
                  ? item.event.presentation?.caption || '业务事件'
                  : item.segment.title;
                const meta = isBusinessEvent
                  ? `BizEvent · seq ${item.event.seq}`
                  : `Segment · coverage [${item.segment.coverage.start_seq}, ${item.segment.coverage.end_seq}]`;
                const description = isBusinessEvent
                  ? item.event.presentation?.body || '暂无可展示内容'
                  : item.segment.conclusion || item.segment.behavior || '暂无分段摘要';

                return {
                  key: item.id,
                  showArrow: false,
                  label: (
                    <div className={`${styles.rawTraceHeader} ${styles.businessTraceHeader}`}>
                      <span className={styles.rawTraceIndex} aria-hidden="true">
                        {index + 1}
                      </span>
                      <span className={styles.businessTraceTitleLine}>
                        <span
                          aria-hidden="true"
                          className={`${styles.rawTraceTypeIcon} ${styles.businessTraceStatusIcon} ${
                            isError ? styles.businessTraceStatusIconError : ''
                          }`}
                        >
                          {isError ? <CloseOutlined /> : <CheckOutlined />}
                        </span>
                        <OverflowTooltip title={title}>
                          <strong>{title}</strong>
                        </OverflowTooltip>
                      </span>
                      <span className={styles.businessTraceMeta}>{meta}</span>
                    </div>
                  ),
                  children: activeBusinessTraceKeySet.has(item.id) ? (
                    <div className={`${styles.rawTraceBody} ${styles.businessTraceBody}`}>
                      <Markdown
                        className={`${styles.rawTraceMarkdown} ${styles.businessTraceMarkdown}`}
                        content={formatSegmentMarkdown(description, '暂无可展示内容')}
                        baseFontSize={12}
                        baseLineHeight={1.6}
                      />
                    </div>
                  ) : null,
                };
              })}
            />
          </div>
        ) : view === 'agent' && rawTraceItems.length ? (
          <div className={styles.rawTraceContainer}>
            <Collapse
              className={styles.rawTraceCollapse}
              bordered={false}
              destroyOnHidden
              activeKey={activeRawTraceKeys}
              onChange={(keys) => setExpandedRawTraceKeys(
                (Array.isArray(keys) ? keys : [keys]).map(String),
              )}
              items={rawTraceItems.map((item, index) => {
                const message = item.input;
                const paired = item.output !== null;
                const expanded = activeRawTraceKeySet.has(item.id);
                return {
                  key: item.id,
                  showArrow: false,
                  label: (
                    <div className={styles.rawTraceHeader}>
                      <span className={styles.rawTraceIndex} aria-hidden="true">
                        {index + 1}
                      </span>
                      <RawTraceTypeIcon type={message.type} />
                      <OverflowTooltip title={message.type}>
                        <strong>{message.type}</strong>
                      </OverflowTooltip>
                      <span className={styles.rawTraceRole}>
                        {paired
                          ? `${message.role || 'system'} → ${item.output?.role || 'tool'}`
                          : message.role || 'system'}
                      </span>
                    </div>
                  ),
                  children: expanded ? (
                    <div className={styles.rawTraceBody}>
                      <Markdown
                        className={styles.rawTraceMarkdown}
                        content={formatRawTraceBody(item)}
                        baseFontSize={12}
                        baseLineHeight={1.6}
                      />
                    </div>
                  ) : null,
                };
              })}
            />
          </div>
        ) : (
          <WorkspaceEmpty
            title={view === 'business' ? '暂无业务轨迹' : '暂无 Agent 原始轨迹'}
            description="后端产生对应 Trace 后会显示在这里。"
          />
        )}
      </section>
    </div>
  );
}

export default function SessionWorkspace({
  activeTab,
  selectedTurnId,
  turns,
  chatProjection,
  sessionId,
  settlementRefreshKey,
  onTabChange,
  onRevealSettlementCards,
  onTurnSelect,
  onPlanEdit,
  onArtifactOpen,
  onArtifactDownload,
  onLoadRawTrace,
}: SessionWorkspaceProps) {
  const [notificationApi, notificationContextHolder] = notification.useNotification();
  const [settlementHighlightKey, setSettlementHighlightKey] = useState(0);
  const handleOpenSettlementCards = useCallback(() => {
    onRevealSettlementCards();
    setSettlementHighlightKey((current) => current + 1);
    notificationApi.destroy(SETTLEMENT_NOTIFICATION_KEY);
  }, [notificationApi, onRevealSettlementCards]);
  const handleNewSettlementCards = useCallback((newCards: SettlementCard[]) => {
    const [firstCard] = [...newCards].sort((left, right) => (
      String(right.created_at ?? '').localeCompare(String(left.created_at ?? ''))
    ));
    if (!firstCard) return;

    const additionalCount = newCards.length - 1;
    notificationApi.info({
      key: SETTLEMENT_NOTIFICATION_KEY,
      message: '发现可沉淀项',
      description: `当前有可沉淀项“${getSettlementCardTitle(firstCard)}”${additionalCount ? `等 ${newCards.length} 项` : ''}，可前往会话空间的“沉淀项”查看。`,
      btn: (
        <Button type="link" size="small" onClick={handleOpenSettlementCards}>
          查看可沉淀项
        </Button>
      ),
      placement: 'topRight',
      duration: 6,
    });
  }, [handleOpenSettlementCards, notificationApi]);
  const settlement = useSettlementCards(sessionId, handleNewSettlementCards);
  const refreshPendingSettlement = settlement.refreshPending;
  const previousSettlementRefreshKeyRef = useRef(settlementRefreshKey);

  useEffect(() => {
    const previousRefreshKey = previousSettlementRefreshKeyRef.current;
    previousSettlementRefreshKeyRef.current = settlementRefreshKey;
    if (
      settlementRefreshKey === undefined
      || previousRefreshKey === undefined
      || previousRefreshKey === settlementRefreshKey
    ) {
      return;
    }
    void refreshPendingSettlement();
  }, [settlementRefreshKey, refreshPendingSettlement]);

  const options = [
    {
      value: 'turns',
      label: <span className={styles.workspaceTabLabel}><BranchesOutlined />任务</span>,
    },
    {
      value: 'artifacts',
      label: <span className={styles.workspaceTabLabel}><FileTextOutlined />成果</span>,
    },
    {
      value: 'settlement',
      label: <span className={styles.workspaceTabLabel}><BulbOutlined />沉淀项</span>,
    },
    {
      value: 'trace',
      label: <span className={styles.workspaceTabLabel}><CodeOutlined />Trace</span>,
    },
  ];

  const content = activeTab === 'turns'
    ? (
        <TasksPanel
          selectedTurnId={selectedTurnId}
          turns={turns}
          chatProjection={chatProjection}
          onTurnSelect={onTurnSelect}
          onPlanEdit={onPlanEdit}
        />
      )
    : activeTab === 'artifacts'
      ? (
          <ArtifactsPanel
            selectedTurnId={selectedTurnId}
            turns={turns}
            chatProjection={chatProjection}
            onTurnSelect={onTurnSelect}
            onArtifactOpen={onArtifactOpen}
            onArtifactDownload={onArtifactDownload}
          />
        )
      : activeTab === 'settlement'
        ? (
            <SettlementPanel
              sessionId={sessionId}
              turns={turns}
              highlightKey={settlementHighlightKey}
              pendingCards={settlement.pendingCards}
              processedCards={settlement.processedCards}
              pendingLoading={settlement.pendingLoading}
              processedLoading={settlement.processedLoading}
              pendingError={settlement.pendingError}
              processedError={settlement.processedError}
              actionableCount={settlement.actionableCount}
              onRefreshPending={settlement.refreshPending}
              onRefreshProcessed={settlement.refreshProcessed}
              onConfirm={settlement.confirm}
              onDismiss={settlement.dismiss}
            />
          )
        : (
          <TracePanel
            selectedTurnId={selectedTurnId}
            turns={turns}
            chatProjection={chatProjection}
            onTurnSelect={onTurnSelect}
            onLoadRawTrace={onLoadRawTrace}
          />
          );

  return (
    <>
      {notificationContextHolder}
      <aside
        className={styles.sessionWorkspace}
        id="session-workspace"
        data-chat-id={chatProjection?.chat.id}
      >
        <div className={styles.workspaceTitle}>
          <div>
            <strong>会话空间</strong>
            <span className={styles.workspaceTitleDescription}>轮次进展、成果、轨迹与可沉淀项</span>
          </div>
        </div>
        <Segmented
          block
          className={styles.workspaceSegmented}
          value={activeTab}
          options={options}
          onChange={(value) => onTabChange(value as WorkspaceTab)}
        />
        <div className={styles.workspaceSegmentContent}>{content}</div>
      </aside>
    </>
  );
}
