import {
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Tooltip } from '@agentscope-ai/design';
import {
  Alert,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  cronApi,
  type CronJob,
  type CronJobDraft,
  type CronJobWriteBody,
} from '@/features/console/api/cron';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import {
  formatCronSchedule,
} from '@/features/console/model/cronSchedule';
import {
  buildCronJobsPath,
  buildNewConsoleSessionPath,
} from '@/features/console/model/sessionNavigation';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import CronJobDrawer from './CronJobDrawer';
import styles from './index.module.less';

interface LocationState {
  draft?: CronJobDraft;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date);
}

export default function CronJobsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { consoleController } = useConsoleLayout();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<CronJob | null>(null);
  const [draft, setDraft] = useState<CronJobDraft | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  const loadJobs = useCallback(async () => {
    const response = await cronApi.listJobs();
    setJobs(response.jobs);
    setLoadError(null);
  }, []);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = '定时任务 · QwenPaw Data';
    void loadJobs()
      .catch((error) => setLoadError(errorMessage(error)))
      .finally(() => setLoading(false));
    return () => {
      document.title = previousTitle;
    };
  }, [loadJobs]);

  useEffect(() => {
    const state = location.state as LocationState | null;
    if (!state?.draft) return;
    setDraft(state.draft);
    setEditing(null);
    setDrawerOpen(true);
    navigate(buildCronJobsPath(), { replace: true, state: null });
  }, [location.state, navigate]);

  const datasourceNames = useMemo(
    () => new Map(
      consoleController.datasources.map((datasource) => [
        datasource.id,
        datasource.name,
      ]),
    ),
    [consoleController.datasources],
  );
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await loadJobs();
    } catch (error) {
      setLoadError(errorMessage(error));
      message.error(`刷新失败：${errorMessage(error)}`);
    } finally {
      setRefreshing(false);
    }
  }, [loadJobs]);

  const replaceInList = useCallback((nextJob: CronJob) => {
    setJobs((current) => current.map((job) => (
      job.id === nextJob.id ? nextJob : job
    )));
  }, []);

  const runAction = useCallback(async (
    key: string,
    action: () => Promise<void>,
  ) => {
    setPendingAction(key);
    try {
      await action();
    } catch (error) {
      message.error(`操作失败：${errorMessage(error)}`);
    } finally {
      setPendingAction(null);
    }
  }, []);

  const handleSubmit = async (body: CronJobWriteBody) => {
    setSubmitting(true);
    try {
      if (editing) {
        const response = await cronApi.replaceJob(editing.id, body);
        replaceInList(response.job);
        message.success('定时任务已保存');
      } else {
        const response = await cronApi.createJob(body);
        setJobs((current) => [response.job, ...current]);
        message.success('定时任务已创建');
      }
      setDrawerOpen(false);
      setEditing(null);
      setDraft(null);
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader
        parentLabel="工作空间"
        title="定时任务"
        onParentClick={() => navigate(buildNewConsoleSessionPath())}
      />

      <main className={styles.main}>
        <div className={styles.tableActions} aria-label="定时任务操作">
          <Space>
            <Button
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={() => void refresh()}
            >
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setDraft(null);
                setEditing(null);
                setDrawerOpen(true);
              }}
            >
              创建任务
            </Button>
          </Space>
        </div>

        {loadError ? (
          <Alert
            className={styles.loadAlert}
            showIcon
            type="error"
            message="定时任务加载失败"
            description={loadError}
            action={<Button size="small" onClick={() => void refresh()}>重试</Button>}
          />
        ) : null}

        <section className={styles.tableCard} aria-label="定时任务列表">
          <Table<CronJob>
            rowKey="id"
            loading={loading}
            dataSource={jobs}
            pagination={false}
            scroll={{ x: 1120 }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="还没有定时任务"
                >
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setDrawerOpen(true)}
                  >
                    创建第一个任务
                  </Button>
                </Empty>
              ),
            }}
            columns={[
              {
                title: '任务名称',
                dataIndex: 'name',
                key: 'name',
                width: 190,
                render: (name: string, job) => (
                  <div className={styles.jobIdentity}>
                    <strong title={name}>{name}</strong>
                    <span>{job.id}</span>
                  </div>
                ),
              },
              {
                title: '状态',
                dataIndex: 'enabled',
                key: 'enabled',
                width: 88,
                render: (enabled: boolean) => (
                  <Tag color={enabled ? 'success' : 'default'}>
                    {enabled ? '已启用' : '已停用'}
                  </Tag>
                ),
              },
              {
                title: '执行计划',
                key: 'schedule',
                width: 180,
                render: (_, job) => (
                  <div className={styles.scheduleCell}>
                    <strong>{formatCronSchedule(job.schedule)}</strong>
                    <span>{job.schedule.timezone}</span>
                  </div>
                ),
              },
              {
                title: '数据源',
                dataIndex: 'datasource_id',
                key: 'datasource_id',
                width: 150,
                render: (datasourceId: string) => (
                  <Tooltip title={datasourceId}>
                    <span className={styles.datasourceCell}>
                      {datasourceNames.get(datasourceId) || datasourceId}
                    </span>
                  </Tooltip>
                ),
              },
              {
                title: '任务指令',
                dataIndex: 'message',
                key: 'message',
                width: 260,
                render: (text: string) => (
                  <Tooltip title={text} placement="topLeft">
                    <span className={styles.messageCell}>{text}</span>
                  </Tooltip>
                ),
              },
              {
                title: '更新时间',
                dataIndex: 'updated_at',
                key: 'updated_at',
                width: 110,
                render: (value: string) => formatUpdatedAt(value),
              },
              {
                title: '操作',
                key: 'actions',
                fixed: 'right',
                width: 260,
                render: (_, job) => {
                  const actionLoading = pendingAction?.endsWith(`:${job.id}`);
                  return (
                    <Space size={0} wrap>
                      <Button
                        type="link"
                        size="small"
                        disabled={actionLoading}
                        onClick={() => {
                          setDraft(null);
                          setEditing(job);
                          setDrawerOpen(true);
                        }}
                      >
                        编辑
                      </Button>
                      <Tooltip title="立即执行只触发一次，不会改变原有计划。">
                        <Button
                          type="link"
                          size="small"
                          loading={pendingAction === `run:${job.id}`}
                          onClick={() => void runAction(`run:${job.id}`, async () => {
                            await cronApi.runJob(job.id);
                            consoleController.reloadSessions();
                            window.setTimeout(
                              () => consoleController.reloadSessions(),
                              1500,
                            );
                            message.success('已触发执行，稍后可在全部会话中查看结果');
                          })}
                        >
                          立即执行
                        </Button>
                      </Tooltip>
                      <Button
                        type="link"
                        size="small"
                        loading={pendingAction === `toggle:${job.id}`}
                        onClick={() => void runAction(`toggle:${job.id}`, async () => {
                          const response = job.enabled
                            ? await cronApi.pauseJob(job.id)
                            : await cronApi.resumeJob(job.id);
                          replaceInList(response.job);
                          message.success(job.enabled ? '任务已停用' : '任务已启用');
                        })}
                      >
                        {job.enabled ? '停用' : '启用'}
                      </Button>
                      <Popconfirm
                        title="删除定时任务"
                        description="删除后将不再按计划执行，确认继续？"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => runAction(`delete:${job.id}`, async () => {
                          await cronApi.deleteJob(job.id);
                          setJobs((current) => current.filter((item) => item.id !== job.id));
                          message.success('定时任务已删除');
                        })}
                      >
                        <Button type="link" size="small" danger disabled={actionLoading}>
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                  );
                },
              },
            ]}
          />
        </section>
      </main>

      <CronJobDrawer
        open={drawerOpen}
        editing={editing}
        draft={draft}
        datasources={consoleController.datasources}
        submitting={submitting}
        onClose={() => {
          setDrawerOpen(false);
          setEditing(null);
          setDraft(null);
        }}
        onSubmit={handleSubmit}
      />
    </div>
  );
}
