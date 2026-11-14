import HistoryLoading from '../HistoryLoading';
import { useHistoryTopScroll } from '../useHistoryTopScroll';
import {
  AppstoreAddOutlined,
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
  ReloadOutlined,
  SearchOutlined,
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
import {
  Empty,
  Pagination,
  Segmented,
  Spin,
  Tabs,
  Tag as AntTag,
  message,
  notification,
} from 'antd';
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, UIEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { dataConsoleApi } from '../../api/client';
import type {
  Artifact as ApiArtifact,
  Plan,
  SegmentArtifact as ApiSegmentArtifact,
} from '../../api/types';
import type { SettlementCard } from '../../api/settlement';
import type { SessionArtifactsState } from '../../model/artifacts/sessionArtifacts';
import {
  filterSessionArtifacts,
  toSessionBusinessArtifact,
} from '../../model/artifacts/sessionArtifacts';
import {
  appendPlanTask,
  buildPlanSubmission,
  createPlanDraft,
  removePlanTask,
  updatePlanTaskSubject,
} from '../../model/plan/planEditor';
import { isCronChannel } from '@/features/console/api/cron';
import { buildCronJobName, buildCronMessage } from '@/features/cron/model/cronMessage';
import type { ChatStreamProjection } from '../../model/runtime/projectStream';
import { serializeTraceContentToMarkdown } from '../../model/artifacts/markdownContent';
import {
  getPluginCallToolName,
  isRawTraceItemFailed,
  selectRawTraceItems,
} from '../../model/runtime/rawTrace';
import { getSettlementCardTitle } from '../../model/settlement/settlementCard';
import { formatSegmentMarkdown } from '../../model/artifacts/segmentMarkdown';
import { selectChatTimeline } from '../../model/runtime/selectChatTimeline';
import { buildCronJobsPath } from '@/shared/navigation/sessionNavigation';
import { useSettlementCards } from '../../model/settlement/useSettlementCards';
import { useSkillProposals } from '../../model/settlement/useSkillProposals';
import { useSkillDrafts } from '../../model/skillDrafts/useSkillDrafts';
import type { Artifact, ConversationTurn, WorkspaceTab } from '../../types';
import ArtifactCard from '../ArtifactCard/index';
import OverflowTooltip from '../OverflowTooltip';
import SettlementPanel from '../SettlementPanel/index';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface SessionWorkspaceProps {
  sessionArtifacts: SessionArtifactsState;
  artifactSessionId?: string | null;
  artifactAccessVersion: string;
  onRefreshSessionArtifacts: () => void;
  historyLoading?: boolean;
  onLoadOlderTurns?: () => Promise<void>;
  readOnly?: boolean; // Shared sessions can view progress, artifacts, and trace only.
  activeTab: WorkspaceTab;
  selectedTurnId: string;
  turns: ConversationTurn[];
  chatProjection: ChatStreamProjection | null;
  sessionId?: string | null;
  sessionChannel?: string | null;
  isReportSession?: boolean;
  settlementRefreshKey?: number;
  skillDraftRefreshKey?: number;
  onTabChange: (tab: WorkspaceTab) => void;
  onClose?: () => void;
  onRevealSettlementCards: () => void;
  onTurnSelect: (turn: ConversationTurn) => void;
  onPlanEdit: (chatId: string, plan: Plan, reason?: string | null) => Promise<void>;
  onArtifactOpen: (artifact: Artifact) => void;
  onArtifactDownload: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
}

interface TurnSelectorProps {
  historyLoading?: boolean;
  onLoadOlderTurns?: () => Promise<void>;
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

const SETTLEMENT_NOTIFICATION_KEY = 'new-settlement-cards';
const ARTIFACT_DEFAULT_PAGE_SIZE = 20;
const ARTIFACT_PAGE_SIZE_OPTIONS = [20, 50];
const ARTIFACT_MAX_MOCK_COUNT = 200;
const TRACE_BATCH_SIZE = 30;
const TRACE_LOAD_MORE_THRESHOLD = 8;

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
  const { t } = useAppI18n();
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
      title={t('workspace.modifyPlan')}
      open
      okText={t('workspace.submitPlan')}
      cancelText={t('common.cancel')}
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
        {chatStatus === 'running' ? t('workspace.planActiveHint') : t('workspace.planFinishedHint')}
      </div>
      <div className={styles.planEditorStepCount}>
        {t('workspace.planStepCount', { count: draft.length })}
      </div>
      <div className={styles.planEditorList}>
        {draft.map((task, index) => {
          const copy =
            task.state === 'completed'
              ? { label: t('workspace.completed'), color: 'success' }
              : task.state === 'in_progress'
                ? { label: t('workspace.running'), color: 'processing' }
                : { label: t('workspace.pending'), color: undefined };
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
                    placeholder={t('workspace.planStepPlaceholder')}
                    aria-label={t('workspace.planStepLabel', { number: index + 1 })}
                    suffix={
                      <Tag bordered={false} color={copy.color} size="small">
                        {copy.label}
                      </Tag>
                    }
                    onChange={(event) =>
                      setDraft((current) =>
                        updatePlanTaskSubject(current, task.id, event.target.value),
                      )
                    }
                  />
                </label>
                <Tooltip title={t('workspace.deletePlanStep', { number: index + 1 })}>
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    aria-label={t('workspace.deletePlanStep', { number: index + 1 })}
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
        {t('workspace.addPlanStep')}
      </Button>
      {error ? (
        <div className={styles.planEditorError} role="alert">
          {error}
        </div>
      ) : null}
    </Modal>
  );
}

function WorkspaceEmpty({ title, description }: WorkspaceEmptyProps) {
  return (
    <Empty
      className={styles.workspaceEmptyState}
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <span className={styles.workspaceEmptyDescription}>
          <strong>{title}</strong>
          <span>{description}</span>
        </span>
      }
    />
  );
}

function RawTraceTypeIcon({ type }: { type: string }) {
  if (type === 'reasoning') {
    return (
      <span
        className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconReasoning}`}
        aria-hidden="true"
      >
        <BulbOutlined />
      </span>
    );
  }

  if (type === 'message') {
    return (
      <span
        className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconMessage}`}
        aria-hidden="true"
      >
        <MessageOutlined />
      </span>
    );
  }

  return (
    <span
      className={`${styles.rawTraceTypeIcon} ${styles.rawTraceTypeIconTool}`}
      aria-hidden="true"
    >
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
    path: artifact.path,
    access: {
      relativePath: artifact.path,
    },
  };
}

function toDisplaySegmentArtifact(
  segmentId: string,
  artifact: ApiSegmentArtifact,
  segments: NonNullable<Artifact['segments']>,
): Artifact {
  const extension = artifact.name.split('.').pop()?.toUpperCase() || 'FILE';
  return {
    id: `${segmentId}:${artifact.relative_path}`,
    name: artifact.name,
    title: artifact.description,
    type: extension,
    meta: artifact.relative_path,
    segments,
    path: artifact.relative_path,
    access: {
      relativePath: artifact.relative_path,
    },
  };
}

function selectArtifacts(
  agentArtifacts: ChatStreamProjection['chat']['artifacts'] | undefined,
  businessSegments: ChatStreamProjection['chat']['segments'] | undefined,
  view: 'business' | 'agent',
): Artifact[] {
  const items: Artifact[] = [];
  const seen = new Set<string>();
  const contextByPath = new Map<string, NonNullable<Artifact['segments']>>();
  const includeOnce = (path: string): boolean => {
    if (seen.has(path)) return false;
    seen.add(path);
    return true;
  };

  for (const [segmentIndex, segment] of (businessSegments ?? []).entries()) {
    for (const artifact of segment.artifact ?? []) {
      const contexts = contextByPath.get(artifact.relative_path) ?? [];
      if (!contexts.some((context) => context.id === segment.segment_id)) {
        contexts.push({
          id: segment.segment_id,
          number: segmentIndex + 1,
          title: segment.title,
        });
      }
      contextByPath.set(artifact.relative_path, contexts);
    }
  }

  if (view === 'agent') {
    for (const artifact of agentArtifacts ?? []) {
      if (!includeOnce(artifact.path)) continue;
      const segments = contextByPath.get(artifact.path);
      items.push({
        ...toDisplayArtifact(artifact),
        segments,
      });
    }
  } else {
    for (const segment of businessSegments ?? []) {
      for (const artifact of segment.artifact ?? []) {
        if (includeOnce(artifact.relative_path)) {
          items.push(
            toDisplaySegmentArtifact(
              segment.segment_id,
              artifact,
              contextByPath.get(artifact.relative_path) ?? [],
            ),
          );
        }
      }
    }
  }

  return items;
}

function getArtifactMockCount(): number {
  if (!import.meta.env.DEV || typeof window === 'undefined') return 0;
  const rawCount = new URLSearchParams(window.location.search).get('mock_artifacts');
  const count = Number.parseInt(rawCount ?? '', 10);
  if (!Number.isFinite(count) || count <= 0) return 0;
  return Math.min(count, ARTIFACT_MAX_MOCK_COUNT);
}

function createArtifactTestMocks(count: number, view: 'business' | 'agent'): Artifact[] {
  const extensions = ['csv', 'sql', 'md', 'json', 'pdf'];
  return Array.from({ length: count }, (_, index) => {
    const number = String(index + 1).padStart(3, '0');
    const extension = extensions[index % extensions.length];
    const inSegment = index % 3 !== 1;
    const longName = index % 5 === 0;
    const name = longName
      ? `demo_app_2025年12月国家维度DAU访问趋势分析_${number}.${extension}`
      : `成果示例_${number}.${extension}`;
    const path = `mock/${view}/${name}`;
    const segments = inSegment
      ? [
          {
            id: `seg-${(index % 4) + 1}`,
            number: (index % 4) + 1,
            title: `${number} 国家维度指标分析与结论沉淀`,
          },
        ]
      : undefined;

    if (segments && index % 11 === 0) {
      segments.push({
        id: `seg-related-${number}`,
        number: 5,
        title: `${number} 异常国家下钻分析`,
      });
    }

    return {
      id: `mock:${view}:${number}`,
      name,
      title: `${view === 'business' ? '业务' : 'Agent'}视图 Mock 成果`,
      type: extension.toUpperCase(),
      meta: path,
      path,
      segments,
      access: { relativePath: path },
    };
  });
}

const TurnSelector = memo(function TurnSelector({
  selectedTurnId,
  turns,
  onTurnSelect,
  onLoadOlderTurns,
  historyLoading,
}: TurnSelectorProps) {
  const { t } = useAppI18n();
  const listRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<HTMLButtonElement>(null);
  useHistoryTopScroll(listRef, Boolean(onLoadOlderTurns), onLoadOlderTurns);
  const previousList = useRef<{ first?: string; height: number; top: number }>();
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const previous = previousList.current;
    if (
      previous?.first &&
      previous.first !== turns[0]?.id &&
      turns.some((turn) => turn.id === previous.first)
    ) {
      list.scrollTop = previous.top + list.scrollHeight - previous.height;
    }
    previousList.current = { first: turns[0]?.id, height: list.scrollHeight, top: list.scrollTop };
    return () => {
      if (previousList.current) previousList.current.top = list.scrollTop;
    };
  }, [turns]);

  useLayoutEffect(() => {
    const list = listRef.current;
    const selected = selectedRef.current;
    if (!list || !selected) return;
    // Scroll only this list; scrollIntoView would also move the conversation page.
    const listRect = list.getBoundingClientRect();
    const itemRect = selected.getBoundingClientRect();
    if (selected === list.lastElementChild) {
      list.scrollTop = list.scrollHeight;
    } else if (itemRect.top < listRect.top) {
      list.scrollTop += itemRect.top - listRect.top;
    } else if (itemRect.bottom > listRect.bottom) {
      list.scrollTop += itemRect.bottom - listRect.bottom;
    }
  }, [selectedTurnId]);

  return (
    <section className={styles.turnSelector} aria-label={t('workspace.selectTurn')}>
      <div className={styles.turnSelectorHeader}>
        <div>
          <strong>{t('workspace.turns')}</strong>
        </div>
        <span>{t('workspace.roundCount', { count: turns.length })}</span>
      </div>

      {historyLoading ? <HistoryLoading /> : null}
      <div className={styles.turnList} ref={listRef}>
        {turns.map((turn) => (
          <button
            className={`${styles.turnItem} ${
              selectedTurnId === turn.id ? styles.turnItemActive : ''
            }`}
            key={turn.id}
            ref={selectedTurnId === turn.id ? selectedRef : undefined}
            aria-current={selectedTurnId === turn.id ? 'step' : undefined}
            type="button"
            onClick={() => onTurnSelect(turn)}
          >
            <span className={styles.turnTitleLine}>
              <span className={styles.turnNumber} aria-hidden="true">
                Chat {turn.number}
              </span>
              <OverflowTooltip title={turn.title}>
                <strong className={styles.turnTitle}>{turn.title}</strong>
              </OverflowTooltip>
            </span>
            <span className={styles.turnMeta}>
              <Tag bordered={false} color="blue" size="small" className={styles.turnDatasourceTag}>
                {turn.dataSource}
              </Tag>
              <time>{turn.sentAt}</time>
            </span>
          </button>
        ))}
        {!turns.length ? (
          <WorkspaceEmpty
            title={t('workspace.noTurns')}
            description={t('workspace.noTurnsDescription')}
          />
        ) : null}
      </div>
    </section>
  );
});

function TurnSelectDropdown({ selectedTurnId, turns, onTurnSelect }: TurnSelectorProps) {
  const { t } = useAppI18n();
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId) ?? turns[0];
  const options = useMemo(
    () =>
      turns.map((turn) => ({
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
      })),
    [turns],
  );

  return (
    <Select
      className={styles.turnSelectControl}
      popupClassName={styles.turnSelectPopup}
      aria-label={t('workspace.selectTurn')}
      value={selectedTurn?.id}
      placeholder={t('workspace.noTurns')}
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
  historyLoading,
  onLoadOlderTurns,
  readOnly,
  selectedTurnId,
  turns,
  chatProjection,
  sessionChannel,
  onTurnSelect,
  onPlanEdit,
}: Pick<
  SessionWorkspaceProps,
  | 'historyLoading'
  | 'onLoadOlderTurns'
  | 'readOnly'
  | 'selectedTurnId'
  | 'turns'
  | 'chatProjection'
  | 'sessionChannel'
  | 'onTurnSelect'
  | 'onPlanEdit'
>) {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const [editingPlan, setEditingPlan] = useState(false);
  const taskTurn = turns.find((turn) => turn.id === selectedTurnId) ?? turns[0];
  const plan = chatProjection?.chat.plan;
  const planSteps = plan?.tasks ?? [];
  const completedCount = planSteps.filter((step) => step.state === 'completed').length;
  const progress = planSteps.length ? Math.round((completedCount / planSteps.length) * 100) : 0;
  const planTitle =
    planSteps.find((step) => step.state !== 'completed')?.subject ??
    planSteps[0]?.subject ??
    t('workspace.executionPlan');

  const handleRefineCron = () => {
    if (readOnly || !chatProjection) return;
    const datasourceId = chatProjection.chat.datasource_id?.trim();
    if (!datasourceId) {
      message.error(t('workspace.selectDatasourceBeforeCron'));
      return;
    }
    const query = chatProjection.chat.user_input || taskTurn?.title || '';
    const channel = sessionChannel && isCronChannel(sessionChannel) ? sessionChannel : 'console';
    navigate(buildCronJobsPath(), {
      state: {
        draft: {
          name: buildCronJobName(query),
          message: buildCronMessage(
            query,
            planSteps.map((step) => step.subject),
          ),
          datasource_id: datasourceId,
          channel,
        },
      },
    });
  };

  return (
    <div className={styles.workspacePanel}>
      <TurnSelector
        historyLoading={historyLoading}
        onLoadOlderTurns={onLoadOlderTurns}
        selectedTurnId={selectedTurnId}
        turns={turns}
        onTurnSelect={onTurnSelect}
      />

      <section className={styles.workspaceContentSection}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>{t('workspace.currentTask')}</strong>
            <OverflowTooltip
              title={
                taskTurn ? `Chat ${taskTurn.number} · ${taskTurn.title}` : t('workspace.notStarted')
              }
            >
              <span>
                {taskTurn
                  ? `Chat ${taskTurn.number} · ${taskTurn.title}`
                  : t('workspace.notStarted')}
              </span>
            </OverflowTooltip>
          </div>
        </div>

        {planSteps.length > 0 ? (
          <div className={styles.planPanel}>
            <div className={styles.planHeader}>
              <div>
                <span className={styles.planKicker}>
                  {chatProjection?.chat.kind === 'planned'
                    ? t('workspace.complexPlan')
                    : t('workspace.executionPlan')}
                </span>
                <OverflowTooltip title={planTitle}>
                  <strong>{planTitle}</strong>
                </OverflowTooltip>
              </div>
              {!readOnly ? (
                <Tooltip title={t('workspace.modifyPlan')}>
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    aria-label={t('workspace.modifyPlan')}
                    onClick={() => setEditingPlan(true)}
                  />
                </Tooltip>
              ) : null}
            </div>

            <div className={styles.planProgress}>
              <Progress
                percent={progress}
                showInfo={false}
                strokeColor="var(--ant-color-primary)"
              />
              <span>
                {t('workspace.completedProgress', {
                  completed: completedCount,
                  total: planSteps.length,
                })}
              </span>
            </div>

            <div className={styles.planSteps}>
              {planSteps.map((step) => {
                const status =
                  step.state === 'completed'
                    ? 'done'
                    : step.state === 'in_progress'
                      ? 'running'
                      : 'todo';
                return (
                  <div
                    className={`${styles.planStep} ${styles[`planStep_${status}`]}`}
                    key={step.id}
                  >
                    <OverflowTooltip title={step.subject}>
                      <span className={styles.planStepTitle}>{step.subject}</span>
                    </OverflowTooltip>
                    {status === 'done' ? <CheckCircleFilled /> : null}
                    {status === 'running' ? <LoadingOutlined spin /> : null}
                  </div>
                );
              })}
            </div>

            {!readOnly ? (
              <Button
                type="primary"
                className={styles.cronButton}
                icon={<ClockCircleOutlined />}
                onClick={() => void handleRefineCron()}
              >
                {t('workspace.refineCron')}
              </Button>
            ) : null}
          </div>
        ) : (
          <WorkspaceEmpty
            title={t('workspace.noPlan')}
            description={t('workspace.noPlanDescription')}
          />
        )}
        {!readOnly && editingPlan && plan && chatProjection ? (
          <PlanEditorModal
            key={chatProjection.chat.id}
            chatLabel={
              taskTurn ? `Chat ${taskTurn.number} · ${taskTurn.title}` : t('workspace.currentChat')
            }
            chatStatus={chatProjection.chat.status}
            plan={plan}
            onCancel={() => setEditingPlan(false)}
            onSubmit={async (nextPlan) => {
              await onPlanEdit(chatProjection.chat.id, nextPlan, '用户在会话空间修改执行计划');
              setEditingPlan(false);
            }}
          />
        ) : null}
      </section>
    </div>
  );
}

type ArtifactsPanelProps = Pick<
  SessionWorkspaceProps,
  | 'turns'
  | 'selectedTurnId'
  | 'chatProjection'
  | 'onTurnSelect'
  | 'onArtifactOpen'
  | 'onArtifactDownload'
  | 'onArtifactShare'
  | 'sessionArtifacts'
  | 'artifactSessionId'
  | 'onRefreshSessionArtifacts'
>;

function ArtifactsPanel(props: ArtifactsPanelProps) {
  const { t } = useAppI18n();
  const [scope, setScope] = useState<'session' | 'turn'>('session');
  return (
    <Tabs
      className={styles.artifactScopePanel}
      size="small"
      activeKey={scope}
      destroyOnHidden
      animated={false}
      tabBarGutter={24}
      items={[
        {
          label: t('workspace.entireSession'),
          key: 'session',
          children: <SessionArtifactsPanel {...props} />,
        },
        {
          label: t('workspace.currentTurn'),
          key: 'turn',
          children: <TurnArtifactsPanel {...props} />,
        },
      ]}
      onChange={(value) => setScope(value as 'session' | 'turn')}
    />
  );
}

function SessionArtifactsPanel({
  sessionArtifacts: state,
  artifactSessionId,
  onRefreshSessionArtifacts,
  onArtifactOpen,
  onArtifactDownload,
}: ArtifactsPanelProps) {
  const { t } = useAppI18n();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(ARTIFACT_DEFAULT_PAGE_SIZE);
  const [view, setView] = useState<'business' | 'agent'>('business');
  const [query, setQuery] = useState('');
  const matches = !!artifactSessionId && state.scope?.sessionId === artifactSessionId;
  const allArtifacts = useMemo(
    () =>
      !matches
        ? []
        : view === 'business'
          ? state.segmentArtifacts.map(toSessionBusinessArtifact)
          : state.artifacts.map(toDisplayArtifact),
    [matches, state.artifacts, state.segmentArtifacts, view],
  );
  const artifacts = useMemo(
    () => filterSessionArtifacts(allArtifacts, query),
    [allArtifacts, query],
  );
  const loading = !!artifactSessionId && (!matches || state.loading);
  const failed = matches && state.error;
  const unavailable = matches && state.unavailable;
  const complete = matches && state.hasLoaded;
  const currentPage = Math.min(page, Math.max(1, Math.ceil(artifacts.length / pageSize)));
  useEffect(() => {
    setPage(currentPage);
  }, [currentPage]);
  const status = !artifactSessionId
    ? t('workspace.sessionArtifactsDraft')
    : unavailable
      ? t('sessionShare.unavailable')
      : failed
        ? t(
            complete
              ? 'workspace.sessionArtifactsRefreshFailed'
              : 'workspace.sessionArtifactsLoadFailed',
          )
        : loading
          ? null
          : !complete
            ? t('workspace.sessionArtifactsIncomplete')
            : null;
  return (
    <div className={`${styles.workspacePanel} ${styles.sessionArtifactsPanel}`}>
      <section
        className={`${styles.workspaceContentSection} ${styles.workspaceArtifactsContentSection}`}
      >
        <div className={styles.sessionArtifactHeader}>
          <strong>{t('workspace.sessionArtifacts')}</strong>
          <div className={styles.sessionArtifactActions}>
            <div className={styles.sessionArtifactSearch}>
              <Input
                allowClear
                prefix={<SearchOutlined />}
                placeholder={t('workspace.searchSessionArtifacts')}
                aria-label={t('workspace.searchSessionArtifacts')}
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
              />
            </div>
            <Tooltip title={t(failed ? 'common.retry' : 'common.refresh')}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                aria-label={t(failed ? 'common.retry' : 'common.refresh')}
                loading={loading}
                disabled={!artifactSessionId || !matches}
                onClick={onRefreshSessionArtifacts}
              />
            </Tooltip>
          </div>
        </div>
        <Segmented
          block
          className={styles.workspaceModeSegmented}
          value={view}
          options={[
            { label: t('workspace.businessView'), value: 'business' },
            { label: t('workspace.agentView'), value: 'agent' },
          ]}
          onChange={(value) => {
            setView(value as 'business' | 'agent');
            setPage(1);
          }}
        />
        {status ? (
          <div className={styles.sessionArtifactStatus} role={failed ? 'alert' : 'status'}>
            {status}
          </div>
        ) : null}
        <Spin spinning={loading} wrapperClassName={styles.sessionArtifactLoading}>
          <div className={styles.artifactListViewport} aria-busy={loading}>
            {artifacts.length ? (
              <div
                className={styles.artifactList}
                role="list"
                aria-label={t('workspace.artifactList')}
              >
                {artifacts
                  .slice((currentPage - 1) * pageSize, currentPage * pageSize)
                  .map((artifact) => (
                    <div key={artifact.path} role="listitem">
                      <ArtifactCard
                        actionsMode="menu"
                        artifact={artifact}
                        showContextTags={view === 'business'}
                        onPreview={() => onArtifactOpen(artifact)}
                        onDownload={() => onArtifactDownload(artifact)}
                      />
                    </div>
                  ))}
              </div>
            ) : complete && !loading && !failed && !unavailable ? (
              <WorkspaceEmpty
                title={t(
                  query.trim()
                    ? 'workspace.noMatchingSessionArtifacts'
                    : view === 'business'
                      ? 'workspace.noSessionBusinessArtifacts'
                      : 'workspace.noSessionArtifacts',
                )}
                description={t(
                  query.trim()
                    ? 'workspace.adjustSessionArtifactSearch'
                    : view === 'business'
                      ? 'workspace.noSessionBusinessArtifactsDescription'
                      : 'workspace.noSessionArtifactsDescription',
                )}
              />
            ) : null}
          </div>
        </Spin>
        {artifacts.length > 0 ? (
          <Pagination
            className={styles.artifactPagination}
            current={currentPage}
            pageSize={pageSize}
            pageSizeOptions={ARTIFACT_PAGE_SIZE_OPTIONS}
            total={artifacts.length}
            size="small"
            simple={{ readOnly: true }}
            showSizeChanger={{ popupClassName: styles.sessionArtifactPageSizeDropdown }}
            showTotal={(total) => t('common.countItems', { count: total })}
            onChange={(nextPage, nextSize) => {
              setPage(nextSize === pageSize ? nextPage : 1);
              setPageSize(nextSize);
            }}
          />
        ) : null}
      </section>
    </div>
  );
}

function TurnArtifactsPanel({
  turns,
  selectedTurnId,
  chatProjection,
  onTurnSelect,
  onArtifactOpen,
  onArtifactDownload,
  onArtifactShare,
}: Pick<
  SessionWorkspaceProps,
  | 'turns'
  | 'selectedTurnId'
  | 'chatProjection'
  | 'onTurnSelect'
  | 'onArtifactOpen'
  | 'onArtifactDownload'
  | 'onArtifactShare'
>) {
  const { t } = useAppI18n();
  const [view, setView] = useState<'business' | 'agent'>('business');
  const [segmentOnly, setSegmentOnly] = useState(false);
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId);
  const chatId = chatProjection?.chat.id;
  const [pagination, setPagination] = useState({ chatId, page: 1 });
  const [pageSize, setPageSize] = useState(ARTIFACT_DEFAULT_PAGE_SIZE);
  const page = pagination.chatId === chatId ? pagination.page : 1;
  const setPage = (nextPage: number) => setPagination({ chatId, page: nextPage });
  const agentArtifacts = chatProjection?.chat.artifacts;
  const businessSegments = chatProjection?.chat.segments;
  const mockArtifactCount = getArtifactMockCount();
  const artifacts = useMemo(
    () =>
      mockArtifactCount > 0
        ? createArtifactTestMocks(mockArtifactCount, view)
        : selectArtifacts(agentArtifacts, businessSegments, view),
    [agentArtifacts, businessSegments, mockArtifactCount, view],
  );
  const filteredArtifacts = useMemo(() => {
    if (!segmentOnly) return artifacts;
    return artifacts.filter((artifact) => Boolean(artifact.segments?.length));
  }, [artifacts, segmentOnly]);
  const pageCount = Math.max(1, Math.ceil(filteredArtifacts.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const showArtifactPagination = filteredArtifacts.length > pageSize;
  const pageArtifacts = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredArtifacts.slice(start, start + pageSize);
  }, [currentPage, filteredArtifacts, pageSize]);

  const handleViewChange = (nextView: 'business' | 'agent') => {
    setView(nextView);
    setPage(1);
  };

  const handlePageChange = (nextPage: number, nextPageSize: number) => {
    if (nextPageSize !== pageSize) {
      setPageSize(nextPageSize);
      setPage(1);
      return;
    }
    setPage(nextPage);
  };

  const handleSegmentFilterChange = (checked: boolean) => {
    setSegmentOnly(checked);
    setPage(1);
  };
  const segmentFilterA11yProps = {
    role: 'checkbox',
    'aria-checked': segmentOnly,
    tabIndex: 0,
    onKeyDown: (event: KeyboardEvent<HTMLSpanElement>) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      handleSegmentFilterChange(!segmentOnly);
    },
  };

  return (
    <div className={`${styles.workspacePanel} ${styles.workspaceArtifactsPanel}`}>
      <TurnSelectDropdown
        selectedTurnId={selectedTurnId}
        turns={turns}
        onTurnSelect={onTurnSelect}
      />

      <section
        className={`${styles.workspaceContentSection} ${styles.workspaceArtifactsContentSection}`}
      >
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>{t('workspace.analysisArtifacts')}</strong>
            <span>
              {selectedTurn ? `Chat ${selectedTurn.number}` : t('workspace.noTurnSelected')}
              {mockArtifactCount > 0
                ? ` · ${t('workspace.mockItems', { count: mockArtifactCount })}`
                : ''}
            </span>
          </div>
        </div>
        <Segmented
          block
          className={styles.workspaceModeSegmented}
          value={view}
          options={[
            { label: t('workspace.businessView'), value: 'business' },
            { label: t('workspace.agentView'), value: 'agent' },
          ]}
          onChange={(value) => handleViewChange(value as 'business' | 'agent')}
        />

        {artifacts.length ? (
          <>
            <div className={styles.artifactFilterBar}>
              <span className={styles.artifactFilterLabel}>{t('workspace.filter')}</span>
              <AntTag.CheckableTag
                {...segmentFilterA11yProps}
                className={styles.artifactFilterTag}
                checked={segmentOnly}
                onChange={handleSegmentFilterChange}
              >
                {t('workspace.segmentArtifacts')}
              </AntTag.CheckableTag>
              <span className={styles.artifactFilterCount}>
                {t('workspace.artifactTotal', { count: filteredArtifacts.length })}
              </span>
            </div>

            <div className={styles.artifactListViewport}>
              {pageArtifacts.length ? (
                <div
                  className={styles.artifactList}
                  role="list"
                  aria-label={t('workspace.artifactList')}
                >
                  {pageArtifacts.map((artifact) => (
                    <div key={artifact.id} role="listitem">
                      <ArtifactCard
                        actionsMode="menu"
                        artifact={artifact}
                        showContextTags
                        onPreview={() => onArtifactOpen(artifact)}
                        onDownload={() => onArtifactDownload(artifact)}
                        onShare={
                          view === 'business' && onArtifactShare
                            ? () => onArtifactShare(artifact)
                            : undefined
                        }
                      />
                    </div>
                  ))}
                </div>
              ) : (
                <WorkspaceEmpty
                  title={t('workspace.noFilteredArtifacts')}
                  description={t('workspace.adjustArtifactFilter')}
                />
              )}
            </div>

            {showArtifactPagination ? (
              <Pagination
                className={styles.artifactPagination}
                current={currentPage}
                pageSize={pageSize}
                pageSizeOptions={ARTIFACT_PAGE_SIZE_OPTIONS}
                total={filteredArtifacts.length}
                size="small"
                simple={{ readOnly: true }}
                showSizeChanger
                showTotal={(total) => t('common.countItems', { count: total })}
                onChange={handlePageChange}
              />
            ) : null}
          </>
        ) : (
          <div className={styles.artifactListViewport}>
            <WorkspaceEmpty
              title={t('workspace.noArtifacts')}
              description={t('workspace.noArtifactsDescription')}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function TracePanel({
  turns,
  selectedTurnId,
  chatProjection,
  sessionId,
  onTurnSelect,
}: Pick<
  SessionWorkspaceProps,
  'turns' | 'selectedTurnId' | 'chatProjection' | 'sessionId' | 'onTurnSelect'
>) {
  const { t } = useAppI18n();
  const [view, setView] = useState<'business' | 'agent'>('business');
  const [expandedRawTraceKeys, setExpandedRawTraceKeys] = useState<string[]>([]);
  const [expandedBusinessTraceState, setExpandedBusinessTraceState] = useState<{
    chatId: string | null;
    keys: string[];
  }>({ chatId: null, keys: [] });
  const [visibleBusinessTraceState, setVisibleBusinessTraceState] = useState<{
    chatId: string | null;
    count: number;
  }>({ chatId: null, count: TRACE_BATCH_SIZE });
  const [visibleRawTraceState, setVisibleRawTraceState] = useState<{
    chatId: string | null;
    count: number;
  }>({ chatId: null, count: TRACE_BATCH_SIZE });
  const selectedTurn = turns.find((turn) => turn.id === selectedTurnId);
  const businessTimeline = useMemo(
    () =>
      chatProjection
        ? selectChatTimeline(chatProjection, { keepCoveredBizEvents: true }).filter(
            (item) => item.kind === 'biz_event' || item.kind === 'segment',
          )
        : [],
    [chatProjection],
  );
  const rawMessages = useMemo(() => chatProjection?.messages ?? [], [chatProjection?.messages]);
  const rawTraceItems = useMemo(() => selectRawTraceItems(rawMessages), [rawMessages]);
  const rawMessageKeys = useMemo(() => rawTraceItems.map((item) => item.id), [rawTraceItems]);
  const rawMessageKeySet = useMemo(() => new Set(rawMessageKeys), [rawMessageKeys]);
  const activeRawTraceKeys = expandedRawTraceKeys.filter((key) => rawMessageKeySet.has(key));
  const activeRawTraceKeySet = useMemo(() => new Set(activeRawTraceKeys), [activeRawTraceKeys]);
  const businessTraceKeys = useMemo(
    () => businessTimeline.map((item) => item.id),
    [businessTimeline],
  );
  const businessTraceKeySet = useMemo(() => new Set(businessTraceKeys), [businessTraceKeys]);
  const currentChatId = chatProjection?.chat.id ?? null;
  const activeBusinessTraceKeys = useMemo(
    () =>
      expandedBusinessTraceState.chatId === currentChatId
        ? expandedBusinessTraceState.keys.filter((key) => businessTraceKeySet.has(key))
        : [],
    [businessTraceKeySet, currentChatId, expandedBusinessTraceState],
  );
  const activeBusinessTraceKeySet = useMemo(
    () => new Set(activeBusinessTraceKeys),
    [activeBusinessTraceKeys],
  );
  const visibleBusinessTraceCount =
    visibleBusinessTraceState.chatId === currentChatId
      ? visibleBusinessTraceState.count
      : TRACE_BATCH_SIZE;
  const visibleRawTraceCount =
    visibleRawTraceState.chatId === currentChatId ? visibleRawTraceState.count : TRACE_BATCH_SIZE;
  const visibleBusinessTimeline = useMemo(
    () => businessTimeline.slice(0, visibleBusinessTraceCount),
    [businessTimeline, visibleBusinessTraceCount],
  );
  const visibleRawTraceItems = useMemo(
    () => rawTraceItems.slice(0, visibleRawTraceCount),
    [rawTraceItems, visibleRawTraceCount],
  );
  const hasMoreBusinessTraces = visibleBusinessTimeline.length < businessTimeline.length;
  const hasMoreRawTraces = visibleRawTraceItems.length < rawTraceItems.length;

  const handleTraceScroll = (event: UIEvent<HTMLDivElement>) => {
    const { clientHeight, scrollHeight, scrollTop } = event.currentTarget;
    if (scrollHeight - scrollTop - clientHeight > TRACE_LOAD_MORE_THRESHOLD) return;

    if (view === 'business' && visibleBusinessTraceCount < businessTimeline.length) {
      setVisibleBusinessTraceState((current) => {
        const currentCount = current.chatId === currentChatId ? current.count : TRACE_BATCH_SIZE;
        return {
          chatId: currentChatId,
          count: Math.min(currentCount + TRACE_BATCH_SIZE, businessTimeline.length),
        };
      });
      return;
    }

    if (view === 'agent' && visibleRawTraceCount < rawTraceItems.length) {
      setVisibleRawTraceState((current) => {
        const currentCount = current.chatId === currentChatId ? current.count : TRACE_BATCH_SIZE;
        return {
          chatId: currentChatId,
          count: Math.min(currentCount + TRACE_BATCH_SIZE, rawTraceItems.length),
        };
      });
    }
  };

  const handleTraceViewChange = (nextView: 'business' | 'agent') => {
    setExpandedBusinessTraceState({ chatId: currentChatId, keys: [] });
    setExpandedRawTraceKeys([]);
    setVisibleBusinessTraceState({ chatId: currentChatId, count: TRACE_BATCH_SIZE });
    setVisibleRawTraceState({ chatId: currentChatId, count: TRACE_BATCH_SIZE });
    if (sessionId && nextView !== view) {
      void dataConsoleApi
        .reportBehaviorEvent({
          kind: 'trace_view',
          session_id: sessionId,
          chat_id: currentChatId,
          payload: { view: nextView },
        })
        .catch(() => undefined);
    }
    setView(nextView);
  };

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
            <strong>{t('workspace.traces')}</strong>
            <OverflowTooltip title={selectedTurn?.title || t('workspace.noTurnSelected')}>
              <span className={styles.workspaceSelectedTurnTitle}>
                {selectedTurn?.title || t('workspace.noTurnSelected')}
              </span>
            </OverflowTooltip>
          </div>
        </div>
        <Segmented
          block
          className={`${styles.workspaceModeSegmented} ${styles.traceModeSegmented}`}
          value={view}
          options={[
            { label: t('workspace.businessTrace'), value: 'business' },
            { label: t('workspace.agentRawTrace'), value: 'agent' },
          ]}
          onChange={(value) => handleTraceViewChange(value as 'business' | 'agent')}
        />

        {(
          view === 'business' ? activeBusinessTraceKeys.length > 1 : activeRawTraceKeys.length > 1
        ) ? (
          <div className={styles.rawTraceActions}>
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
              {t('workspace.collapseAll')}
            </Button>
          </div>
        ) : null}

        {view === 'business' && businessTimeline.length ? (
          <div key="business" className={styles.rawTraceContainer} onScroll={handleTraceScroll}>
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
              items={visibleBusinessTimeline.map((item, index) => {
                const isBusinessEvent = item.kind === 'biz_event';
                const isError = isBusinessEvent && item.event.status === 'error';
                const title = isBusinessEvent
                  ? item.event.presentation?.caption || t('workspace.businessEvent')
                  : item.segment.title;
                const description = isBusinessEvent
                  ? item.event.presentation?.body || t('workspace.noDisplayContent')
                  : item.segment.conclusion ||
                    item.segment.behavior ||
                    t('workspace.noSegmentSummary');
                const isExpanded = activeBusinessTraceKeySet.has(item.id);

                return {
                  key: item.id,
                  showArrow: false,
                  label: (
                    <div className={`${styles.rawTraceHeader} ${styles.businessTraceHeader}`}>
                      <span className={styles.businessTraceTitleLine}>
                        <span
                          aria-hidden="true"
                          className={`${styles.rawTraceTypeIcon} ${styles.businessTraceStatusIcon} ${
                            isError ? styles.businessTraceStatusIconError : ''
                          }`}
                        >
                          {isError ? <CloseOutlined /> : <CheckOutlined />}
                        </span>
                        <span className={styles.traceSequenceText}>{index + 1}</span>
                        <OverflowTooltip title={isExpanded ? title : `${title} ${description}`}>
                          <span className={styles.businessTraceSummaryLine}>
                            <span className={styles.businessTraceTitle}>{title}</span>
                            {!isExpanded ? (
                              <span className={styles.businessTraceSummary}>{description}</span>
                            ) : null}
                          </span>
                        </OverflowTooltip>
                      </span>
                    </div>
                  ),
                  children: isExpanded ? (
                    <div className={`${styles.rawTraceBody} ${styles.businessTraceBody}`}>
                      <Markdown
                        className={`${styles.rawTraceMarkdown} ${styles.businessTraceMarkdown}`}
                        content={formatSegmentMarkdown(
                          description,
                          t('workspace.noDisplayContent'),
                        )}
                        baseFontSize={12}
                        baseLineHeight={1.6}
                      />
                    </div>
                  ) : null,
                };
              })}
            />
            {hasMoreBusinessTraces ? (
              <div className={styles.traceLoadMore} aria-label={t('common.loading')}>
                <LoadingOutlined spin aria-hidden="true" />
                <span>{t('common.loading')}</span>
              </div>
            ) : null}
          </div>
        ) : view === 'agent' && rawTraceItems.length ? (
          <div key="agent" className={styles.rawTraceContainer} onScroll={handleTraceScroll}>
            <Collapse
              className={styles.rawTraceCollapse}
              bordered={false}
              destroyOnHidden
              activeKey={activeRawTraceKeys}
              onChange={(keys) =>
                setExpandedRawTraceKeys((Array.isArray(keys) ? keys : [keys]).map(String))
              }
              items={visibleRawTraceItems.map((item, index) => {
                const message = item.input;
                const paired = item.output !== null;
                const toolName = getPluginCallToolName(message);
                const traceMeta =
                  toolName ??
                  (paired
                    ? `${message.role || 'system'} → ${item.output?.role || 'tool'}`
                    : message.role || 'system');
                const body = paired
                  ? [
                      '### Input',
                      serializeTraceContentToMarkdown(message.content) ||
                        `_${t('workspace.noContent')}_`,
                      '### Output',
                      serializeTraceContentToMarkdown(item.output?.content ?? []) ||
                        `_${t('workspace.noContent')}_`,
                    ].join('\n\n')
                  : serializeTraceContentToMarkdown(message.content) ||
                    `_${t('workspace.noContent')}_`;
                return {
                  key: item.id,
                  showArrow: false,
                  label: (
                    <div className={styles.rawTraceHeader}>
                      <RawTraceTypeIcon type={message.type} />
                      <span className={styles.traceSequenceText}>{index + 1}</span>
                      <OverflowTooltip title={message.type}>
                        <span className={styles.rawTraceTitle}>{message.type}</span>
                      </OverflowTooltip>
                      <span className={styles.rawTraceRole}>{traceMeta}</span>
                      {isRawTraceItemFailed(item) ? (
                        <span className={styles.rawTraceFailureBadge}>
                          {t('workspace.traceFailed')}
                        </span>
                      ) : null}
                    </div>
                  ),
                  children: activeRawTraceKeySet.has(item.id) ? (
                    <div className={styles.rawTraceBody}>
                      <Markdown
                        className={styles.rawTraceMarkdown}
                        content={body}
                        baseFontSize={12}
                        baseLineHeight={1.6}
                      />
                    </div>
                  ) : null,
                };
              })}
            />
            {hasMoreRawTraces ? (
              <div className={styles.traceLoadMore} aria-label={t('common.loading')}>
                <LoadingOutlined spin aria-hidden="true" />
                <span>{t('common.loading')}</span>
              </div>
            ) : null}
          </div>
        ) : (
          <WorkspaceEmpty
            title={
              view === 'business' ? t('workspace.noBusinessTrace') : t('workspace.noAgentTrace')
            }
            description={t('workspace.noTraceDescription')}
          />
        )}
      </section>
    </div>
  );
}

export default function SessionWorkspace({
  sessionArtifacts,
  artifactSessionId,
  artifactAccessVersion,
  onRefreshSessionArtifacts,
  historyLoading,
  onLoadOlderTurns,
  readOnly = false,
  activeTab,
  selectedTurnId,
  turns,
  chatProjection,
  sessionId,
  sessionChannel,
  settlementRefreshKey,
  skillDraftRefreshKey,
  onTabChange,
  onClose,
  onRevealSettlementCards,
  onTurnSelect,
  onPlanEdit,
  onArtifactOpen,
  onArtifactDownload,
  onArtifactShare,
}: SessionWorkspaceProps) {
  const { t } = useAppI18n();
  const [notificationApi, notificationContextHolder] = notification.useNotification();
  const [settlementHighlightKey, setSettlementHighlightKey] = useState(0);
  const handleOpenSettlementCards = useCallback(() => {
    onRevealSettlementCards();
    setSettlementHighlightKey((current) => current + 1);
    notificationApi.destroy(SETTLEMENT_NOTIFICATION_KEY);
  }, [notificationApi, onRevealSettlementCards]);
  const handleNewSettlementCards = useCallback(
    (newCards: SettlementCard[]) => {
      const [firstCard] = [...newCards].sort((left, right) =>
        String(right.created_at ?? '').localeCompare(String(left.created_at ?? '')),
      );
      if (!firstCard) return;

      const additionalCount = newCards.length - 1;
      notificationApi.info({
        key: SETTLEMENT_NOTIFICATION_KEY,
        message: t('workspace.newAssetsFound'),
        description: t('workspace.newAssetsDescription', {
          name: getSettlementCardTitle(firstCard),
          more: additionalCount ? t('workspace.moreAssets', { count: newCards.length }) : '',
        }),
        btn: (
          <Button type="link" size="small" onClick={handleOpenSettlementCards}>
            {t('workspace.viewAssets')}
          </Button>
        ),
        placement: 'topRight',
        duration: 6,
      });
    },
    [handleOpenSettlementCards, notificationApi, t],
  );
  const settlement = useSettlementCards(readOnly ? null : sessionId, handleNewSettlementCards);
  const skillProposals = useSkillProposals(readOnly ? null : sessionId);
  const skillDrafts = useSkillDrafts(readOnly ? null : sessionId, skillDraftRefreshKey);
  const refreshPendingSettlement = settlement.refreshPending;
  const refreshSkillProposals = skillProposals.refreshPending;
  const previousSettlementRefreshKeyRef = useRef(settlementRefreshKey);

  useEffect(() => {
    const previousRefreshKey = previousSettlementRefreshKeyRef.current;
    previousSettlementRefreshKeyRef.current = settlementRefreshKey;
    if (
      readOnly ||
      settlementRefreshKey === undefined ||
      previousRefreshKey === undefined ||
      previousRefreshKey === settlementRefreshKey
    ) {
      return;
    }
    void refreshPendingSettlement();
    void refreshSkillProposals();
  }, [readOnly, settlementRefreshKey, refreshPendingSettlement, refreshSkillProposals]);

  const hasSettlementCards =
    settlement.pendingCards.length > 0 || settlement.processedCards.length > 0;
  const hasSettlementItems =
    hasSettlementCards || skillProposals.pending.length > 0 || skillDrafts.drafts.length > 0;
  const settlementItemsLoading =
    settlement.pendingLoading ||
    settlement.processedLoading ||
    skillProposals.loading ||
    skillDrafts.loading;
  const showSettlementTab =
    !readOnly && (hasSettlementItems || (activeTab === 'settlement' && settlementItemsLoading));

  useEffect(() => {
    if (
      activeTab === 'settlement' &&
      (readOnly || (!settlementItemsLoading && !hasSettlementItems))
    ) {
      onTabChange('turns');
    }
  }, [activeTab, readOnly, hasSettlementItems, onTabChange, settlementItemsLoading]);

  const options = [
    {
      value: 'turns',
      label: (
        <span className={styles.workspaceTabLabel}>
          <BranchesOutlined />
          {t('workspace.tasks')}
        </span>
      ),
    },
    {
      value: 'artifacts',
      label: (
        <span className={styles.workspaceTabLabel}>
          <FileTextOutlined />
          {t('workspace.artifacts')}
        </span>
      ),
    },
    {
      value: 'trace',
      label: (
        <span className={styles.workspaceTabLabel}>
          <CodeOutlined />
          {t('workspace.traces')}
        </span>
      ),
    },
  ];

  if (showSettlementTab) {
    options.push({
      value: 'settlement',
      label: (
        <span className={styles.workspaceTabLabel}>
          <AppstoreAddOutlined />
          {t('workspace.assets')}
        </span>
      ),
    });
  }

  useEffect(() => {
    if (readOnly) notificationApi.destroy(SETTLEMENT_NOTIFICATION_KEY);
  }, [readOnly, notificationApi]);

  const content =
    activeTab === 'turns' || (readOnly && activeTab === 'settlement') ? (
      <TasksPanel
        historyLoading={historyLoading}
        onLoadOlderTurns={onLoadOlderTurns}
        readOnly={readOnly}
        selectedTurnId={selectedTurnId}
        turns={turns}
        chatProjection={chatProjection}
        sessionChannel={sessionChannel}
        onTurnSelect={onTurnSelect}
        onPlanEdit={onPlanEdit}
      />
    ) : activeTab === 'artifacts' ? (
      <ArtifactsPanel
        key={`${artifactSessionId ?? 'draft'}:${artifactAccessVersion}`}
        sessionArtifacts={sessionArtifacts}
        artifactSessionId={artifactSessionId}
        onRefreshSessionArtifacts={onRefreshSessionArtifacts}
        selectedTurnId={selectedTurnId}
        turns={turns}
        chatProjection={chatProjection}
        onTurnSelect={onTurnSelect}
        onArtifactOpen={onArtifactOpen}
        onArtifactDownload={onArtifactDownload}
        onArtifactShare={onArtifactShare}
      />
    ) : activeTab === 'settlement' ? (
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
        skillDrafts={skillDrafts.drafts}
        skillDraftLoading={skillDrafts.loading}
        skillDraftError={skillDrafts.error}
        onRefreshSkillDrafts={skillDrafts.refresh}
        skillPending={skillProposals.pending}
        skillLoading={skillProposals.loading}
        skillError={skillProposals.error}
        onRefreshSkill={skillProposals.refreshPending}
        onAcceptSkill={skillProposals.accept}
        onRejectSkill={skillProposals.reject}
      />
    ) : (
      <TracePanel
        selectedTurnId={selectedTurnId}
        turns={turns}
        chatProjection={chatProjection}
        sessionId={sessionId}
        onTurnSelect={onTurnSelect}
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
            <strong>{t('workspace.title')}</strong>
          </div>
          {onClose ? (
            <Button
              className={styles.workspaceMobileClose}
              type="text"
              size="small"
              icon={<CloseOutlined />}
              aria-label={t('console.collapseWorkspace')}
              onClick={onClose}
            />
          ) : null}
        </div>
        <Segmented
          block
          className={styles.workspaceSegmented}
          value={activeTab}
          options={options}
          onChange={(value) => {
            if (value === 'trace' && activeTab !== 'trace' && sessionId) {
              void dataConsoleApi
                .reportBehaviorEvent({
                  kind: 'trace_view',
                  session_id: sessionId,
                  chat_id: chatProjection?.chat.id ?? null,
                  payload: { view: 'business' },
                })
                .catch(() => undefined);
            }
            onTabChange(value as WorkspaceTab);
          }}
        />
        <div className={styles.workspaceSegmentContent}>{content}</div>
      </aside>
    </>
  );
}
