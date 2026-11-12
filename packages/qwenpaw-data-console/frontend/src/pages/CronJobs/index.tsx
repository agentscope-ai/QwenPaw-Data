import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { Button, Tooltip } from '@agentscope-ai/design';
import { Alert, Empty, Popconfirm, Space, Table, Tag, message, Input, Select } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  cronApi,
  type CronJob,
  type CronJobDraft,
  type CronJobWriteBody,
  CRON_CHANNELS,
} from '@/features/console/api/cron';
import { channelConfigApi } from '@/features/console/api/channelConfig';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import { formatCronSchedule } from '@/features/cron/model/cronSchedule';
import { buildCronJobsPath } from '@/shared/navigation/sessionNavigation';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import { useAppI18n } from '@/i18n';
import type { MessageKey } from '@/i18n/locales';
import CronJobDrawer from './CronJobDrawer';
import styles from './index.module.less';

const CHANNEL_LABEL: Record<string, MessageKey> = {
  console: 'cron.channel.console',
  feishu: 'cron.channel.feishu',
  dingtalk: 'cron.channel.dingtalk',
  wecom: 'cron.channel.wecom',
  wechat: 'cron.channel.wechat',
};

interface LocationState {
  draft?: CronJobDraft;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatUpdatedAt(value: string, language: 'zh' | 'en'): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
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
  const { language, t } = useAppI18n();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [channelEnabledStatus, setChannelEnabledStatus] = useState<Map<string, boolean>>(new Map());
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<CronJob | null>(null);
  const [draft, setDraft] = useState<CronJobDraft | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [jobStatus, setJobStatus] = useState('');
  const [channel, setChannel] = useState('');
  const isFirstLoad = useRef(true);

  var channelOptions = [{ value: '', label: t('cron.allChannelTypes') }];
  CRON_CHANNELS.map((c) => channelOptions.push({ value: c, label: t(CHANNEL_LABEL[c]) }));

  const loadJobs = useCallback(
    async (signal?: AbortSignal) => {
      const enabled = jobStatus === 'enabled' ? true : jobStatus === 'disabled' ? false : undefined;
      const response = await cronApi.listJobs(
        {
          keyword: keyword.trim() || undefined,
          enabled,
          channel: channel || undefined,
        },
        signal,
      );
      setJobs(response.jobs);
      const channelConfigResponse = await channelConfigApi.get();
      setLoadError(null);
      const channelEnabled = new Map<string, boolean>();
      channelEnabled.set('feishu', channelConfigResponse?.feishu?.enabled || false);
      channelEnabled.set('dingtalk', channelConfigResponse?.dingtalk?.enabled || false);
      channelEnabled.set('wecom', channelConfigResponse?.wecom?.enabled || false);
      channelEnabled.set('wechat', channelConfigResponse?.wechat?.enabled || false);
      channelEnabled.set('console', true);
      setChannelEnabledStatus(channelEnabled);
    },
    [keyword, jobStatus, channel],
  );

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('cron.title')} · QwenPaw Data`;
    return () => {
      document.title = previousTitle;
    };
  }, [t]);

  // Load when page loads. Delay loading upon filter changes to avoid loading on every key stroke.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const delay = isFirstLoad.current ? 0 : 300;
    isFirstLoad.current = false;
    setLoading(true);
    const timer = setTimeout(() => {
      void loadJobs(controller.signal)
        .catch((error) => {
          if (!cancelled) setLoadError(errorMessage(error));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, delay);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
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
    () =>
      new Map(consoleController.datasources.map((datasource) => [datasource.id, datasource.name])),
    [consoleController.datasources],
  );
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await loadJobs();
    } catch (error) {
      setLoadError(errorMessage(error));
      message.error(t('common.refreshFailed', { message: errorMessage(error) }));
    } finally {
      setRefreshing(false);
    }
  }, [loadJobs, t]);

  const replaceInList = useCallback((nextJob: CronJob) => {
    setJobs((current) => current.map((job) => (job.id === nextJob.id ? nextJob : job)));
  }, []);

  const runAction = useCallback(
    async (key: string, action: () => Promise<void>) => {
      setPendingAction(key);
      try {
        await action();
      } catch (error) {
        message.error(t('common.operationFailed', { message: errorMessage(error) }));
      } finally {
        setPendingAction(null);
      }
    },
    [t],
  );

  const handleSubmit = async (body: CronJobWriteBody) => {
    setSubmitting(true);
    try {
      if (editing) {
        const response = await cronApi.replaceJob(editing.id, body);
        replaceInList(response.job);
        message.success(t('cron.saved'));
      } else {
        const response = await cronApi.createJob(body);
        setJobs((current) => [response.job, ...current]);
        message.success(t('cron.created'));
      }
      setDrawerOpen(false);
      setEditing(null);
      setDraft(null);
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('cron.title')} description={t('cron.description')} />

      <main className={styles.main}>
        <div className={styles.actionBar}>
          <section className={styles.filters} aria-label={t('cron.title')}>
            <Input
              aria-label={t('cron.filterKeyword')}
              style={{ width: 320 }}
              allowClear
              prefix={<SearchOutlined />}
              placeholder={t('cron.filterKeyword')}
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
            <Select
              aria-label={t('cron.allJobStatuses')}
              style={{ width: 160 }}
              value={jobStatus}
              options={[
                { value: '', label: t('cron.allJobStatuses') },
                { value: 'enabled', label: t('cron.filterEnabled') },
                { value: 'disabled', label: t('cron.filterDisabled') },
              ]}
              onChange={(value) => setJobStatus(value)}
            />
            <Select
              aria-label={t('cron.allChannelTypes')}
              style={{ width: 160 }}
              value={channel}
              options={channelOptions}
              onChange={(value) => setChannel(value)}
            />
          </section>
          <div className={styles.tableActions} aria-label={t('cron.actions')}>
            <Space>
              <Button
                aria-label={t('common.refresh')}
                title={t('common.refresh')}
                icon={<ReloadOutlined />}
                loading={refreshing}
                onClick={() => void refresh()}
              />
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  setDraft(null);
                  setEditing(null);
                  setDrawerOpen(true);
                }}
              >
                {t('cron.create')}
              </Button>
            </Space>
          </div>
        </div>

        {loadError ? (
          <Alert
            className={styles.loadAlert}
            showIcon
            type="error"
            message={t('cron.loadFailed')}
            description={loadError}
            action={
              <Button size="small" onClick={() => void refresh()}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : null}

        <div className={styles.tableSummary}>{t('common.countItems', { count: jobs.length })}</div>

        <section className={styles.tableCard} aria-label={t('cron.list')}>
          <Table<CronJob>
            rowKey="id"
            loading={loading}
            dataSource={jobs}
            pagination={false}
            scroll={{ x: 1360 }}
            locale={{
              emptyText: (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('cron.empty')} />
              ),
            }}
            columns={[
              {
                title: t('cron.name'),
                dataIndex: 'name',
                key: 'name',
                width: 190,
                render: (name: string, job) => (
                  <div className={styles.jobIdentity}>
                    <strong title={name}>{name}</strong>
                    <Tooltip title={job.id}>
                      <span>{job.id}</span>
                    </Tooltip>
                  </div>
                ),
              },
              {
                title: t('cron.status'),
                dataIndex: 'enabled',
                key: 'enabled',
                width: 88,
                render: (enabled: boolean) => (
                  <Tag color={enabled ? 'success' : 'default'}>
                    {enabled ? t('common.enabled') : t('common.disabled')}
                  </Tag>
                ),
              },
              {
                title: t('cron.schedule'),
                key: 'schedule',
                width: 180,
                render: (_, job) => {
                  const scheduleText = formatCronSchedule(job.schedule, language);
                  return (
                    <div className={styles.scheduleCell}>
                      <Tooltip title={scheduleText}>
                        <strong>{scheduleText}</strong>
                      </Tooltip>
                      <span>{job.schedule.timezone}</span>
                    </div>
                  );
                },
              },
              {
                title: t('cron.datasource'),
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
                title: t('cron.channel'),
                dataIndex: 'channel',
                key: 'channel',
                width: 110,
                render: (channel: string) => (
                  <span
                    title={channelEnabledStatus?.get(channel) ? '' : t('cron.channelDisabled')}
                    className={
                      channelEnabledStatus?.get(channel)
                        ? styles.channelCell
                        : styles.channelCellDisabled
                    }
                  >
                    <ChannelIcon
                      channel={channel}
                      size={16}
                      className={channelEnabledStatus?.get(channel) ? undefined : styles.grayedOut}
                    />
                    {CHANNEL_LABEL[channel] ? t(CHANNEL_LABEL[channel]) : channel}
                  </span>
                ),
              },
              {
                title: t('cron.instruction'),
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
                title: t('cron.session'),
                dataIndex: 'session_id',
                key: 'session_id',
                width: 160,
                render: (sessionId: string | null, job) => {
                  const text = sessionId || job.target_external_key;
                  return (
                    <Tooltip title={text || undefined}>
                      <span className={styles.datasourceCell}>{text || '—'}</span>
                    </Tooltip>
                  );
                },
              },
              {
                title: t('cron.updatedAt'),
                dataIndex: 'updated_at',
                key: 'updated_at',
                width: 110,
                render: (value: string) => formatUpdatedAt(value, language),
              },
              {
                title: t('cron.action'),
                key: 'actions',
                fixed: 'right',
                width: 240,
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
                        {t('common.edit')}
                      </Button>
                      <Tooltip
                        title={
                          channelEnabledStatus?.get(job.channel)
                            ? t('cron.runNowHint')
                            : t('cron.channelDisabled')
                        }
                      >
                        <Button
                          type="link"
                          size="small"
                          disabled={actionLoading || !channelEnabledStatus?.get(job.channel)}
                          loading={pendingAction === `run:${job.id}`}
                          onClick={() =>
                            void runAction(`run:${job.id}`, async () => {
                              await cronApi.runJob(job.id);
                              consoleController.reloadSessions();
                              window.setTimeout(() => consoleController.reloadSessions(), 1500);
                              message.success(t('cron.runTriggered'));
                            })
                          }
                        >
                          {t('cron.runNow')}
                        </Button>
                      </Tooltip>
                      <Tooltip
                        title={
                          !job.enabled && !channelEnabledStatus?.get(job.channel)
                            ? t('cron.channelDisabled')
                            : ''
                        }
                      >
                        <Button
                          type="link"
                          size="small"
                          disabled={
                            actionLoading ||
                            (!job.enabled && !channelEnabledStatus?.get(job.channel))
                          }
                          loading={pendingAction === `toggle:${job.id}`}
                          onClick={() =>
                            void runAction(`toggle:${job.id}`, async () => {
                              const response = job.enabled
                                ? await cronApi.pauseJob(job.id)
                                : await cronApi.resumeJob(job.id);
                              replaceInList(response.job);
                              message.success(job.enabled ? t('cron.disabled') : t('cron.enabled'));
                            })
                          }
                        >
                          {job.enabled ? t('common.disable') : t('common.enable')}
                        </Button>
                      </Tooltip>
                      <Popconfirm
                        title={t('cron.deleteTitle')}
                        description={t('cron.deleteDescription')}
                        okText={t('common.delete')}
                        cancelText={t('common.cancel')}
                        okButtonProps={{ danger: true }}
                        onConfirm={() =>
                          runAction(`delete:${job.id}`, async () => {
                            await cronApi.deleteJob(job.id);
                            setJobs((current) => current.filter((item) => item.id !== job.id));
                            message.success(t('cron.deleted'));
                          })
                        }
                      >
                        <Button type="link" size="small" danger disabled={actionLoading}>
                          {t('common.delete')}
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
