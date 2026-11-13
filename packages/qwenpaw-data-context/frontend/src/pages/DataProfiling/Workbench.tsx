import {
  Alert,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Steps,
  Typography,
  ReloadOutlined,
  message,
} from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns } from '@/design';
import { Segmented } from 'antd';
import ManualModelingPanel from './components/ManualModelingPanel';
import StageAiDrawer from './components/StageAiDrawer';
import { getModelingPhaseIndex, MODELING_PHASES } from './modelingPhases';
import { PROFILING_STAGE_BY_PATH, PROFILING_STAGES } from './stageConfig';
import type { ProfilingFieldConfig } from './stageConfig';
import ProfilingStatusTag from './ProfilingStatusTag';
import { statusText } from './status';
import {
  addProfilingRow,
  batchConfirmProfilingRows,
  batchSkipProfilingRows,
  completeProfilingStage,
  getProfilingStatus,
  getProfilingTask,
  queryProfilingResults,
  refineProfilingStage,
  rerunProfilingStage,
  skipProfilingStage,
  startProfilingStage,
  updateProfilingRow,
} from '@/services/dataProfiling';
import { useHasCapability } from '@/store/authStore';
import type {
  ProfilingResultRow,
  ProfilingCandidateStagePath,
  ProfilingStagePath,
  ProfilingStatusResponse,
  ProfilingTask,
} from '@/types/dataProfiling';
import type { TFunction } from 'i18next';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import styles from './DataProfiling.module.less';

const TASK_POLL_INTERVAL = 1500;
const SECTION_TITLE_STYLE = { margin: 0, fontSize: 16, lineHeight: '24px' } as const;
type RowFormValues = Record<string, string | number | undefined>;

function isStagePath(value: string | null): value is ProfilingStagePath {
  return Boolean(value && value in PROFILING_STAGE_BY_PATH);
}

function normalizeBooleanValue(value: unknown): 'Y' | 'N' {
  return value === 'Y' ? 'Y' : 'N';
}

function formatListValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>;
          return String(record.source_table ?? record.dataset_name ?? JSON.stringify(record));
        }
        return String(item);
      })
      .join(', ');
  }
  if (value == null || value === '') return '';
  return String(value);
}

function probingAlertMessage(
  t: TFunction,
  task: ProfilingTask | null,
  activePath: ProfilingStagePath,
  hasRows: boolean,
): string {
  if (task?.status === 'STOPPED') {
    return t('dataProfiling.actionSuccess.stop');
  }
  if (task?.status === 'FAILED') {
    return task.error_msg || task.message || t('dataProfiling.probing');
  }
  if (task?.status === 'RUNNING') {
    const pct = typeof task.progress === 'number' ? `${task.progress}% ` : '';
    return `${pct}${task.message || t('dataProfiling.probing')}`.trim();
  }
  return (
    task?.error_msg ||
    task?.message ||
    (hasRows ? t('dataProfiling.probing') : t(`dataProfiling.managementWaiting.${activePath}`))
  );
}

function fieldValue(field: ProfilingFieldConfig, value: unknown) {
  if (field.kind === 'list') {
    const text = formatListValue(value);
    return text || '-';
  }
  if (field.kind === 'boolean') {
    return normalizeBooleanValue(value) === 'Y' ? 'yes' : 'no';
  }
  if (value == null || value === '') return '-';
  return String(value);
}

/** Normalize API row values into form-safe primitives (esp. member_tables JSON arrays). */
function normalizeFormValue(
  field: ProfilingFieldConfig,
  value: unknown,
): string | number | undefined {
  if (field.kind === 'boolean') return normalizeBooleanValue(value);
  if (value == null || value === '') return undefined;
  if (field.kind === 'list') return formatListValue(value) || undefined;
  if (field.kind === 'number') {
    const numeric = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(numeric) ? numeric : undefined;
  }
  if (typeof value === 'number') return value;
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export default function DataProfilingWorkbench() {
  const { t } = useTranslation();
  const { datasourceId = '' } = useParams();
  const currentDatasourceId = datasourceId;
  const [searchParams, setSearchParams] = useSearchParams();
  const datasourceName = searchParams.get('name') || '-';
  const datasourceType = searchParams.get('type') || '-';
  const initialStage = isStagePath(searchParams.get('stage'))
    ? (searchParams.get('stage') as ProfilingStagePath)
    : PROFILING_STAGES[0].path;
  const [activePath, setActivePath] = useState<ProfilingStagePath>(initialStage);
  const [status, setStatus] = useState<ProfilingStatusResponse | null>(null);
  const [task, setTask] = useState<ProfilingTask | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [editingRow, setEditingRow] = useState<ProfilingResultRow | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [aiDrawerOpen, setAiDrawerOpen] = useState(false);
  const [form] = Form.useForm<RowFormValues>();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const canWrite = useHasCapability('datasource:write');
  const navigate = useNavigate();

  const activeConfig = PROFILING_STAGE_BY_PATH[activePath];
  const initialResultKeyword = activeConfig.filterParam
    ? searchParams.get('keyword') ||
      searchParams.get(activeConfig.filterParam) ||
      searchParams.get('filter') ||
      undefined
    : undefined;
  const statusByStage = useMemo(
    () => new Map(status?.stages.map((item) => [item.stage, item]) ?? []),
    [status],
  );
  const activeStageStatus = statusByStage.get(activeConfig.stage)?.status ?? 'NOT_STARTED';
  const currentWorkflowIndex = useMemo(() => {
    const nextIndex = PROFILING_STAGES.findIndex((item) => {
      const stageStatus = statusByStage.get(item.stage)?.status;
      return !['COMPLETED', 'SKIPPED'].includes(stageStatus ?? 'NOT_STARTED');
    });
    return nextIndex < 0 ? PROFILING_STAGES.length - 1 : nextIndex;
  }, [statusByStage]);
  const activePhaseIndex = getModelingPhaseIndex(activePath);
  const currentPhaseIndex = getModelingPhaseIndex(PROFILING_STAGES[currentWorkflowIndex].path);
  const canOpenAiStage = useCallback(
    (path: ProfilingStagePath) => {
      // Manual modeling presents dictionaries before calibers. Their AI drawers
      // must be accessible as soon as the corresponding modeling phase is reached.
      if (path === 'dimension-manage' || path === 'metric-manage') {
        return getModelingPhaseIndex(path) <= currentPhaseIndex;
      }
      return PROFILING_STAGES.findIndex((item) => item.path === path) <= currentWorkflowIndex;
    },
    [currentPhaseIndex, currentWorkflowIndex],
  );
  const activePhase = MODELING_PHASES[activePhaseIndex];
  const previousPhase = MODELING_PHASES[activePhaseIndex - 1];
  const nextPhase = MODELING_PHASES[activePhaseIndex + 1];
  const activePhaseStatuses = activePhase.stagePaths.map(
    (path) => statusByStage.get(PROFILING_STAGE_BY_PATH[path].stage)?.status ?? 'NOT_STARTED',
  );
  const isActivePhaseComplete = activePhaseStatuses.every((value) =>
    ['COMPLETED', 'SKIPPED'].includes(value),
  );
  const isActivePhaseProbing = activePhaseStatuses.some((value) => value === 'PROBING');
  const isActivePhaseReviewing = activePhaseStatuses.some((value) => value === 'REVIEWING');
  const phaseActionLabel = nextPhase
    ? t(
        isActivePhaseComplete
          ? 'dataProfiling.phaseActions.next'
          : 'dataProfiling.phaseActions.completeAndNext',
        {
          current: t(activePhase.titleKey),
          next: t(nextPhase.titleKey),
        },
      )
    : t('dataProfiling.phaseActions.completeAll', { current: t(activePhase.titleKey) });
  const phaseActionHint = t(
    isActivePhaseProbing
      ? 'dataProfiling.phaseActionHints.probing'
      : isActivePhaseReviewing
        ? 'dataProfiling.phaseActionHints.reviewing'
        : isActivePhaseComplete
          ? 'dataProfiling.phaseActionHints.completed'
          : 'dataProfiling.phaseActionHints.ready',
    { current: t(activePhase.titleKey) },
  );
  const canEditRows = canWrite && activeStageStatus === 'REVIEWING';
  const canSkipStage = !['COMPLETED', 'SKIPPED'].includes(activeStageStatus);
  const taskTerminal = Boolean(task && ['FAILED', 'STOPPED', 'SUCCESS'].includes(task.status));
  const displayedStageStatus =
    activeStageStatus === 'PROBING' && taskTerminal && task ? task.status : activeStageStatus;

  const goToStage = useCallback(
    (path: ProfilingStagePath, filter?: string) => {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set('stage', path);
      nextParams.set('datasource_id', currentDatasourceId);
      if (filter) nextParams.set('filter', filter);
      else nextParams.delete('filter');
      setSearchParams(nextParams, { replace: true });
    },
    [currentDatasourceId, searchParams, setSearchParams],
  );

  const goToNextStage = useCallback(() => {
    const currentIndex = PROFILING_STAGES.findIndex((item) => item.path === activePath);
    const next = PROFILING_STAGES[currentIndex + 1];
    if (next) goToStage(next.path);
  }, [activePath, goToStage]);

  const goToPreviousPhase = useCallback(() => {
    if (!previousPhase) return;
    const previousStep = previousPhase.manualSteps[previousPhase.manualSteps.length - 1];
    goToStage(previousStep.stagePath);
  }, [goToStage, previousPhase]);

  const saveAndExit = useCallback(() => {
    message.success(t('dataProfiling.phaseActions.savedAndExit'));
    navigate('/data-profiling');
  }, [navigate, t]);

  const loadStatus = useCallback(async () => {
    const next = await getProfilingStatus(currentDatasourceId);
    setStatus(next);
    return next;
  }, [currentDatasourceId]);

  useEffect(() => {
    setStatus(null);
    setTask(null);
    setSelectedRowKeys([]);
    setEditingRow(null);
    setEditorOpen(false);
    setAiDrawerOpen(false);
    form.resetFields();
  }, [currentDatasourceId, form]);

  useEffect(() => {
    let disposed = false;
    setStatusLoading(true);
    void getProfilingStatus(currentDatasourceId)
      .then((next) => {
        if (!disposed) setStatus(next);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!disposed) setStatusLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [currentDatasourceId]);

  useEffect(() => {
    if (!status) return;
    const requestedStageValue = searchParams.get('stage');
    const requestedStage = isStagePath(requestedStageValue) ? requestedStageValue : null;
    const requestedIndex = requestedStage
      ? PROFILING_STAGES.findIndex((item) => item.path === requestedStage)
      : -1;
    const nextPath = PROFILING_STAGES[currentWorkflowIndex].path;
    const targetPath =
      requestedStage && requestedIndex <= currentWorkflowIndex ? requestedStage : nextPath;
    const hasDatasourceContext = searchParams.get('datasource_id') === currentDatasourceId;
    if (activePath !== targetPath) {
      setActivePath(targetPath);
    }
    if (!hasDatasourceContext || requestedStageValue !== targetPath) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set('stage', targetPath);
      nextParams.set('datasource_id', currentDatasourceId);
      nextParams.delete('filter');
      setSearchParams(nextParams, { replace: true });
    }
  }, [
    activePath,
    currentDatasourceId,
    currentWorkflowIndex,
    searchParams,
    setSearchParams,
    status,
  ]);

  useEffect(() => {
    setTask(null);
    setSelectedRowKeys([]);
    setEditingRow(null);
    setEditorOpen(false);
    form.resetFields();
  }, [activePath, form]);

  const isAiStageEnabled = useCallback(
    (path: ProfilingStagePath) =>
      PROFILING_STAGES.findIndex((item) => item.path === path) <= currentWorkflowIndex,
    [currentWorkflowIndex],
  );

  const getAiActionLabel = useCallback(
    (path: ProfilingStagePath) => {
      const stageStatus = statusByStage.get(PROFILING_STAGE_BY_PATH[path].stage)?.status;
      if (stageStatus === 'PROBING') return t('dataProfiling.aiActions.viewProgress');
      if (stageStatus === 'REVIEWING') return t('dataProfiling.aiActions.reviewResults');
      return t('dataProfiling.useAiModeling');
    },
    [statusByStage, t],
  );

  const openAiStage = useCallback(
    (path: ProfilingStagePath) => {
      if (!canOpenAiStage(path)) return;
      const targetPath = isAiStageEnabled(path)
        ? path
        : MODELING_PHASES[getModelingPhaseIndex(path)].stagePaths.find(isAiStageEnabled);
      if (!targetPath) return;
      goToStage(targetPath);
      setAiDrawerOpen(true);
    },
    [canOpenAiStage, goToStage, isAiStageEnabled],
  );

  useEffect(() => {
    if (!aiDrawerOpen) return;
    void loadStatus();
    actionRef.current?.reload();
  }, [activePath, aiDrawerOpen, loadStatus]);

  const hydrateEditorForm = useCallback(
    (record: ProfilingResultRow | null) => {
      form.resetFields();
      if (!record) return;
      const values = Object.fromEntries(
        activeConfig.fields.map((field) => [
          field.name,
          normalizeFormValue(field, record[field.name]),
        ]),
      );
      form.setFieldsValue(values);
    },
    [activeConfig, form],
  );

  useEffect(() => {
    if (activeStageStatus !== 'PROBING' || taskTerminal) return undefined;
    let disposed = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const nextTask = await getProfilingTask(currentDatasourceId, activePath);
        if (disposed) return;
        setTask(nextTask);
        if (nextTask.status === 'RUNNING') {
          timer = window.setTimeout(poll, TASK_POLL_INTERVAL);
          return;
        }
        await loadStatus();
        actionRef.current?.reload();
      } catch {
        if (!disposed) timer = window.setTimeout(poll, TASK_POLL_INTERVAL * 2);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [activePath, activeStageStatus, currentDatasourceId, loadStatus, taskTerminal]);

  const refreshWorkbench = useCallback(async () => {
    await loadStatus();
    actionRef.current?.reload();
  }, [loadStatus]);

  const runAction = useCallback(
    async (
      key: string,
      action: () => Promise<unknown>,
      successKey: string,
      onSuccess?: () => void,
    ) => {
      setActionLoading(key);
      try {
        const result = await action();
        if (result && typeof result === 'object' && 'status' in result && 'progress' in result) {
          setTask(result as ProfilingTask);
        }
        message.success(t(successKey));
        await refreshWorkbench();
        onSuccess?.();
      } catch {
        // The shared request layer already surfaces API errors. Keep the current
        // stage and editor state instead of creating an unhandled promise.
      } finally {
        setActionLoading(null);
      }
    },
    [refreshWorkbench, t],
  );

  const completeActivePhase = useCallback(async () => {
    if (isActivePhaseComplete) {
      if (nextPhase) goToStage(nextPhase.stagePaths[0]);
      else navigate('/data-profiling');
      return;
    }
    setActionLoading('complete-phase');
    try {
      for (const path of activePhase.stagePaths) {
        const stage = PROFILING_STAGE_BY_PATH[path].stage;
        const stageStatus = statusByStage.get(stage)?.status ?? 'NOT_STARTED';
        if (['COMPLETED', 'SKIPPED'].includes(stageStatus)) continue;
        // Manual data is already saved. AI is optional, including when it was
        // never started. Apply candidates only through the AI review drawer.
        await skipProfilingStage(currentDatasourceId, path);
      }
      message.success(t('dataProfiling.actionSuccess.complete'));
      await refreshWorkbench();
      if (nextPhase) goToStage(nextPhase.stagePaths[0]);
      else navigate('/data-profiling');
    } catch {
      // A phase can contain multiple backend stages. Refresh even after a
      // partial failure so the progress bar reflects stages already completed.
      await refreshWorkbench().catch(() => undefined);
    } finally {
      setActionLoading(null);
    }
  }, [
    activePhase,
    currentDatasourceId,
    goToStage,
    isActivePhaseComplete,
    navigate,
    nextPhase,
    refreshWorkbench,
    statusByStage,
    t,
  ]);

  const openAddDrawer = () => {
    setEditingRow(null);
    hydrateEditorForm(null);
    setEditorOpen(true);
  };

  const openEditDrawer = useCallback(
    (record: ProfilingResultRow) => {
      setEditingRow(record);
      hydrateEditorForm(record);
      setEditorOpen(true);
    },
    [hydrateEditorForm],
  );

  const saveRow = async () => {
    const values = await form.validateFields();
    setActionLoading('save-row');
    try {
      if (editingRow) {
        const editableValues = Object.fromEntries(
          activeConfig.editable
            .filter(
              (name) => !activeConfig.fields.find((field) => field.name === name)?.disableOnEdit,
            )
            .map((name) => [name, values[name]]),
        );
        await updateProfilingRow(currentDatasourceId, activePath, editingRow.id, editableValues);
        message.success(t('common.editSuccess'));
      } else {
        await addProfilingRow(
          currentDatasourceId,
          activePath as ProfilingCandidateStagePath,
          values,
        );
        message.success(t('common.createSuccess'));
      }
      setEditorOpen(false);
      form.resetFields();
      actionRef.current?.reload();
    } catch {
      // Validation and API errors keep the drawer values in place for correction.
    } finally {
      setActionLoading(null);
    }
  };

  const batchAction = async (
    key: string,
    action: (rowIds: number[]) => Promise<unknown>,
    successKey: string,
  ) => {
    const rowIds = selectedRowKeys.map(Number);
    if (!rowIds.length) {
      message.warning(t('dataProfiling.selectRowsFirst'));
      return;
    }
    setActionLoading(key);
    try {
      await action(rowIds);
      message.success(t(successKey));
      setSelectedRowKeys([]);
      actionRef.current?.reload();
    } catch {
      // The shared request layer already reports the failure.
    } finally {
      setActionLoading(null);
    }
  };

  const resultColumns = useMemo<ProColumns<ProfilingResultRow>[]>(
    () => [
      ...activeConfig.fields
        .filter((field) => !field.disableOnShow)
        .map((field): ProColumns<ProfilingResultRow> => ({
          title: t(`dataProfiling.fields.${field.name}`),
          dataIndex: field.name,
          search: false,
          width: field.kind === 'textarea' || field.kind === 'list' ? 240 : 150,
          ellipsis: field.kind === 'textarea' || field.kind === 'list',
          render: (_, record) => {
            const value = fieldValue(field, record[field.name]);
            if (field.kind === 'boolean') return t(`common.${value}`);
            return value;
          },
        })),
      {
        title: t('dataProfiling.originType'),
        dataIndex: 'origin',
        search: false,
        width: 100,
        render: (_, record) => t(`dataProfiling.origin.${record.origin ?? 'create'}`),
      },
      ...(activeConfig.hasRows
        ? [
            {
              title: t('dataProfiling.sourceType'),
              dataIndex: 'source_type',
              search: false,
              width: 100,
              render: (_, record) => t(`dataProfiling.source.${record.source_type ?? 'ALGO'}`),
            } as ProColumns<ProfilingResultRow>,
            {
              title: t('dataProfiling.rowStatus'),
              dataIndex: 'probe_status',
              search: false,
              width: 110,
              render: (_, record) => {
                const rowStatus = record.probe_status;
                const label =
                  rowStatus && activePath === 'dataset'
                    ? t(`dataProfiling.datasetRowStatus.${rowStatus}`)
                    : undefined;
                return <ProfilingStatusTag t={t} status={rowStatus} label={label} />;
              },
            } as ProColumns<ProfilingResultRow>,
          ]
        : []),
      ...(canEditRows || activePath === 'dimension-manage' || activePath === 'metric-manage'
        ? [
            {
              title: t('common.actions'),
              dataIndex: 'action',
              search: false,
              fixed: 'right' as const,
              width: 210,
              render: (_: unknown, record: ProfilingResultRow) => (
                <Space size={4}>
                  {canEditRows ? (
                    <Button
                      type="link"
                      style={{ paddingInline: 4 }}
                      onClick={(event) => {
                        event.stopPropagation();
                        openEditDrawer(record);
                      }}
                    >
                      {t('common.edit')}
                    </Button>
                  ) : null}
                  {canEditRows && activeConfig.hasRows ? (
                    <Button
                      type="link"
                      style={{ paddingInline: 4 }}
                      onClick={(event) => {
                        event.stopPropagation();
                        Modal.confirm({
                          title: t('dataProfiling.skipRowConfirmTitle'),
                          content: t('dataProfiling.skipRowConfirmContent'),
                          okText: t('dataProfiling.actions.skipRow'),
                          cancelText: t('common.cancel'),
                          onOk: async () => {
                            await batchSkipProfilingRows(
                              currentDatasourceId,
                              activePath as ProfilingCandidateStagePath,
                              [record.id],
                            );
                            message.success(t('dataProfiling.actionSuccess.skipRows'));
                            actionRef.current?.reload();
                          },
                        });
                      }}
                    >
                      {t('dataProfiling.actions.skipRow')}
                    </Button>
                  ) : null}
                  {activePath === 'dimension-manage' ? (
                    <Button
                      type="link"
                      style={{ paddingInline: 4 }}
                      onClick={() =>
                        goToStage('dimension-caliber', String(record.dimension_name ?? ''))
                      }
                    >
                      {t('dataProfiling.actions.backToDimensionCaliber')}
                    </Button>
                  ) : null}
                  {activePath === 'metric-manage' ? (
                    <Button
                      type="link"
                      style={{ paddingInline: 4 }}
                      onClick={() => goToStage('metric-caliber', String(record.metric_name ?? ''))}
                    >
                      {t('dataProfiling.actions.backToMetricCaliber')}
                    </Button>
                  ) : null}
                </Space>
              ),
            },
          ]
        : []),
    ],
    [activeConfig, activePath, canEditRows, currentDatasourceId, goToStage, openEditDrawer, t],
  );

  const showRerun =
    activeStageStatus !== 'NOT_STARTED' && (activeStageStatus !== 'PROBING' || taskTerminal);
  const showRefine =
    ['REVIEWING', 'COMPLETED'].includes(activeStageStatus) &&
    (activeStageStatus !== 'PROBING' || taskTerminal);

  return (
    <Space direction="vertical" size={16} className={styles.workbenchLayout}>
      <Card className={styles.summaryCard}>
        <div className={styles.summaryHeader}>
          <div className={styles.datasourceMeta}>
            <span>
              {t('fields.dataSourceName')}：{datasourceName}
            </span>
            <span>
              {t('dataProfiling.dataSourceIdentifier')}：{currentDatasourceId}
            </span>
            <span>
              {t('fields.dataSourceType')}：{datasourceType}
            </span>
          </div>
          <div className={styles.overallActions}>
            <div className={styles.overallStatus}>
              <Typography.Text className={styles.overallLabel}>
                {t('dataProfiling.overallStatus')}
              </Typography.Text>
              <ProfilingStatusTag t={t} status={status?.overall_status} />
            </div>
            <Button
              icon={<ReloadOutlined />}
              loading={statusLoading}
              onClick={() => void refreshWorkbench()}
            >
              {t('common.refresh')}
            </Button>
          </div>
        </div>
        <div className={styles.stepsPanel}>
          <Steps
            className={styles.profilingSteps}
            responsive
            current={activePhaseIndex}
            onChange={(index) => {
              if (index > currentPhaseIndex) return;
              const phase = MODELING_PHASES[index];
              const next =
                phase.stagePaths.find((path) => {
                  const config = PROFILING_STAGE_BY_PATH[path];
                  const phaseStageStatus = statusByStage.get(config.stage)?.status;
                  return !['COMPLETED', 'SKIPPED'].includes(phaseStageStatus ?? 'NOT_STARTED');
                }) ?? phase.stagePaths[phase.stagePaths.length - 1];
              goToStage(next);
            }}
            items={MODELING_PHASES.map((phase, index) => {
              const phaseStatuses = phase.stagePaths.map(
                (path) =>
                  statusByStage.get(PROFILING_STAGE_BY_PATH[path].stage)?.status ?? 'NOT_STARTED',
              );
              const isFinished = phaseStatuses.every((value) =>
                ['COMPLETED', 'SKIPPED'].includes(value),
              );
              const phaseStarted =
                index === currentPhaseIndex ||
                phaseStatuses.some((value) => value !== 'NOT_STARTED');
              const phaseDisplayStatus = isFinished
                ? 'COMPLETED'
                : phaseStarted
                  ? 'IN_PROGRESS'
                  : 'NOT_STARTED';
              const isActive = index === currentPhaseIndex || phaseDisplayStatus !== 'NOT_STARTED';
              return {
                title: <span className={styles.stepTitle}>{t(phase.titleKey)}</span>,
                description: (
                  <span className={styles.stepDescription}>
                    {statusText(t, phaseDisplayStatus)}
                  </span>
                ),
                status: isFinished
                  ? ('finish' as const)
                  : isActive
                    ? ('process' as const)
                    : ('wait' as const),
                disabled: index > currentPhaseIndex,
              };
            })}
          />
        </div>
      </Card>

      {status && searchParams.get('datasource_id') === currentDatasourceId ? (
        <ManualModelingPanel
          key={currentDatasourceId}
          phaseIndex={activePhaseIndex}
          datasourceId={currentDatasourceId}
          canUseAi={canWrite}
          isAiStageEnabled={canOpenAiStage}
          getAiActionLabel={getAiActionLabel}
          onOpenAi={openAiStage}
          showPhaseAction
          previousPhaseDisabled={!previousPhase}
          onPreviousPhase={goToPreviousPhase}
          onSaveExit={saveAndExit}
          phaseActionLabel={phaseActionLabel}
          phaseActionHint={phaseActionHint}
          phaseActionLoading={actionLoading === 'complete-phase'}
          phaseActionDisabled={!canWrite || statusLoading || !status || Boolean(actionLoading)}
          onPhaseAction={() => void completeActivePhase()}
        />
      ) : null}

      <StageAiDrawer
        open={aiDrawerOpen}
        title={`${getAiActionLabel(activePath)} · ${t(activeConfig.titleKey)}`}
        onClose={() => setAiDrawerOpen(false)}
      >
        {activePhase.stagePaths.length > 1 ? (
          <Segmented
            className={styles.aiStageSelector}
            value={activePath}
            options={activePhase.stagePaths.map((path) => ({
              label: t(PROFILING_STAGE_BY_PATH[path].titleKey),
              value: path,
              disabled: !isAiStageEnabled(path),
            }))}
            onChange={(value: string | number) => goToStage(value as ProfilingStagePath)}
          />
        ) : null}
        <Card className={styles.stageCard}>
          <Space direction="vertical" size={16} className={styles.stageContent}>
            <div className={styles.stageHeader}>
              <div className={styles.stageCopy}>
                <div className={styles.stageTitleRow}>
                  <Typography.Title level={5} style={SECTION_TITLE_STYLE}>
                    {t(activeConfig.titleKey)}
                  </Typography.Title>
                  <ProfilingStatusTag t={t} status={displayedStageStatus} />
                </div>
                <Typography.Text type="secondary" className={styles.stageDescription}>
                  {t(activeConfig.descriptionKey)}
                </Typography.Text>
              </div>
              {canWrite && status ? (
                <Space wrap className={styles.stageActions}>
                  {activeStageStatus === 'NOT_STARTED' ? (
                    <Button
                      type="primary"
                      loading={actionLoading === 'start'}
                      onClick={() =>
                        void runAction(
                          'start',
                          () => startProfilingStage(currentDatasourceId, activePath),
                          'dataProfiling.actionSuccess.start',
                        )
                      }
                    >
                      {t('dataProfiling.actions.start')}
                    </Button>
                  ) : null}
                  {canSkipStage ? (
                    <Button
                      loading={actionLoading === 'skip'}
                      onClick={() =>
                        void runAction(
                          'skip',
                          () => skipProfilingStage(currentDatasourceId, activePath),
                          'dataProfiling.actionSuccess.skip',
                          goToNextStage,
                        )
                      }
                    >
                      {t('dataProfiling.actions.skip')}
                    </Button>
                  ) : null}
                  {showRefine ? (
                    <Button
                      loading={actionLoading === 'refine'}
                      onClick={() =>
                        void runAction(
                          'refine',
                          () => refineProfilingStage(currentDatasourceId, activePath),
                          'dataProfiling.actionSuccess.refine',
                        )
                      }
                    >
                      {t('dataProfiling.actions.update')}
                    </Button>
                  ) : null}
                  {showRerun ? (
                    <Button
                      loading={actionLoading === 'rerun'}
                      onClick={() =>
                        Modal.confirm({
                          title: t('dataProfiling.rerunConfirmTitle'),
                          content: t('dataProfiling.rerunConfirmContent'),
                          okText: t('dataProfiling.actions.rerun'),
                          cancelText: t('common.cancel'),
                          onOk: () =>
                            runAction(
                              'rerun',
                              () => rerunProfilingStage(currentDatasourceId, activePath),
                              'dataProfiling.actionSuccess.rerun',
                            ),
                        })
                      }
                    >
                      {t('dataProfiling.actions.rerun')}
                    </Button>
                  ) : null}
                  {activeStageStatus === 'REVIEWING' ? (
                    <Button
                      type="primary"
                      loading={actionLoading === 'complete'}
                      onClick={() =>
                        void runAction(
                          'complete',
                          () => completeProfilingStage(currentDatasourceId, activePath),
                          'dataProfiling.actionSuccess.complete',
                          goToNextStage,
                        )
                      }
                    >
                      {t('dataProfiling.actions.complete')}
                    </Button>
                  ) : null}
                </Space>
              ) : null}
            </div>

            {activeStageStatus === 'PROBING' ? (
              <Alert
                type={task?.status === 'FAILED' ? 'error' : 'info'}
                showIcon
                message={probingAlertMessage(t, task, activePath, Boolean(activeConfig.hasRows))}
              />
            ) : null}

            {activeStageStatus === 'NOT_STARTED' ? (
              <div className={styles.notStartedEmpty}>
                <Typography.Text type="secondary">
                  {t('dataProfiling.notStartedTip')}
                </Typography.Text>
              </div>
            ) : null}

            {activeStageStatus === 'SKIPPED' ? (
              <Alert type="info" showIcon message={t('dataProfiling.skippedTip')} />
            ) : null}

            {!canWrite ? (
              <Alert type="info" showIcon message={t('dataProfiling.readOnlyTip')} />
            ) : null}
          </Space>
        </Card>

        {['REVIEWING', 'COMPLETED'].includes(activeStageStatus) ? (
          <ProTable<ProfilingResultRow>
            key={activePath}
            rowKey="id"
            actionRef={actionRef}
            headerTitle={t('dataProfiling.results')}
            search={false}
            columns={[
              ...(activeConfig.filterParam
                ? [
                    {
                      title: t(`dataProfiling.fields.${activeConfig.filterParam}`),
                      dataIndex: 'keyword',
                      hideInTable: true,
                      initialValue: initialResultKeyword,
                    } as ProColumns<ProfilingResultRow>,
                  ]
                : []),
              ...resultColumns,
            ]}
            scroll={{
              x: Math.max(1100, activeConfig.fields.filter((f) => !f.disableOnShow).length * 170),
            }}
            rowSelection={
              activeConfig.hasRows && canEditRows
                ? {
                    selectedRowKeys,
                    preserveSelectedRowKeys: true,
                    onChange: setSelectedRowKeys,
                  }
                : undefined
            }
            toolBarRender={() =>
              canEditRows
                ? [
                    ...(activeConfig.canAdd
                      ? [
                          <Button key="add" onClick={openAddDrawer}>
                            {t('common.add')}
                          </Button>,
                        ]
                      : []),
                    ...(activeConfig.hasRows
                      ? [
                          <Button
                            key="confirm"
                            type="primary"
                            disabled={!selectedRowKeys.length}
                            loading={actionLoading === 'batch-confirm'}
                            onClick={() =>
                              void batchAction(
                                'batch-confirm',
                                (ids) =>
                                  batchConfirmProfilingRows(
                                    currentDatasourceId,
                                    activePath as ProfilingCandidateStagePath,
                                    ids,
                                  ),
                                'dataProfiling.actionSuccess.confirmRows',
                              )
                            }
                          >
                            {t('dataProfiling.actions.confirmRows')}
                          </Button>,
                          <Button
                            key="skip-rows"
                            disabled={!selectedRowKeys.length}
                            loading={actionLoading === 'batch-skip'}
                            onClick={() =>
                              void batchAction(
                                'batch-skip',
                                (ids) =>
                                  batchSkipProfilingRows(
                                    currentDatasourceId,
                                    activePath as ProfilingCandidateStagePath,
                                    ids,
                                  ),
                                'dataProfiling.actionSuccess.skipRows',
                              )
                            }
                          >
                            {t('dataProfiling.actions.skipRows')}
                          </Button>,
                        ]
                      : []),
                  ]
                : []
            }
            request={async (params) => {
              const queryParams = {
                page: params.current,
                size: params.pageSize,
                ...(activeConfig.filterParam && (params.keyword || initialResultKeyword)
                  ? { [activeConfig.filterParam]: params.keyword || initialResultKeyword }
                  : {}),
              };
              const result = await queryProfilingResults(
                currentDatasourceId,
                activePath,
                queryParams,
              );
              return { data: result.records, total: result.total, success: true };
            }}
            pagination={{ defaultPageSize: 20, showSizeChanger: true }}
          />
        ) : null}
      </StageAiDrawer>

      <Drawer
        placement="right"
        width={640}
        open={editorOpen}
        title={t(editingRow ? 'dataProfiling.editRow' : 'dataProfiling.addRow')}
        onClose={() => {
          setEditorOpen(false);
          form.resetFields();
        }}
        afterOpenChange={(open) => {
          // destroyOnHidden remounts Form after open; re-hydrate once fields are mounted.
          if (open) hydrateEditorForm(editingRow);
        }}
        destroyOnHidden
        footer={
          <div style={{ textAlign: 'right' }}>
            <Space>
              <Button
                onClick={() => {
                  setEditorOpen(false);
                  form.resetFields();
                }}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="primary"
                loading={actionLoading === 'save-row'}
                onClick={() => void saveRow()}
              >
                {t('common.confirm')}
              </Button>
            </Space>
          </div>
        }
      >
        <Form form={form} layout="vertical" preserve={false}>
          {activeConfig.fields
            .filter(
              (field) =>
                activeConfig.editable.includes(field.name) || (editingRow && field.readOnly),
            )
            .map((field) => {
              const name = field.name;
              const fieldDisabled = field.readOnly || Boolean(editingRow && field.disableOnEdit);
              return (
                <Form.Item
                  key={name}
                  name={name}
                  label={t(`dataProfiling.fields.${name}`)}
                  rules={
                    field.required
                      ? [{ required: true, message: t('dataProfiling.fieldRequired') }]
                      : undefined
                  }
                >
                  {field.kind === 'textarea' || field.kind === 'list' ? (
                    <Input.TextArea rows={3} disabled={fieldDisabled} />
                  ) : field.kind === 'number' ? (
                    <InputNumber style={{ width: '100%' }} disabled={fieldDisabled} />
                  ) : field.kind === 'boolean' ? (
                    <Select
                      options={[
                        { label: t('common.yes'), value: 'Y' },
                        { label: t('common.no'), value: 'N' },
                      ]}
                      disabled={fieldDisabled}
                    />
                  ) : (
                    <Input disabled={fieldDisabled} />
                  )}
                </Form.Item>
              );
            })}
        </Form>
      </Drawer>
    </Space>
  );
}
