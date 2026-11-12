import { Button, Input, Select } from '@agentscope-ai/design';
import {
  AutoComplete,
  DatePicker,
  Form,
  InputNumber,
  Space,
  Switch,
  TimePicker,
  message,
} from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { useEffect, useMemo, useState } from 'react';
import {
  cronApi,
  isCronImChannel,
  type CronJob,
  type CronJobDraft,
  type CronJobWriteBody,
  type CronScheduleType,
  type CronTarget,
} from '@/features/console/api/cron';
import {
  channelConfigApi,
  type ChannelConfigResponse,
  type ChannelKey,
} from '@/features/console/api/channelConfig';
import { dataConsoleApi } from '@/features/console/api/client';
import type { ConsoleSession, DatasourceOption } from '@/features/console/api/types';
import type { MessageKey } from '@/i18n/locales';
import {
  buildCronExpression,
  CRON_DAYS,
  isoToWallTimeInput,
  parseCronPreset,
  wallTimeToIso,
  type CronDay,
  type CronFrequency,
  type CronIntervalUnit,
} from '@/features/cron/model/cronSchedule';
import RouteDrawer from '@/features/console/components/RouteDrawer';
import styles from './index.module.less';
import { useAppI18n } from '@/i18n';

interface CronJobDrawerProps {
  open: boolean;
  editing: CronJob | null;
  draft: CronJobDraft | null;
  datasources: DatasourceOption[];
  submitting: boolean;
  onClose: () => void;
  onSubmit: (body: CronJobWriteBody) => Promise<void>;
}

const IM_CHANNELS: ChannelKey[] = ['feishu', 'dingtalk', 'wecom', 'wechat'];

const CHANNEL_LABEL: Record<string, MessageKey> = {
  console: 'cron.channel.console',
  feishu: 'cron.channel.feishu',
  dingtalk: 'cron.channel.dingtalk',
  wecom: 'cron.channel.wecom',
  wechat: 'cron.channel.wechat',
};

function channelLabel(t: (key: MessageKey) => string, channel: string): string {
  const key = CHANNEL_LABEL[channel];
  return key ? t(key) : channel;
}

interface FormValues {
  name: string;
  enabled: boolean;
  datasource_id: string;
  channel: string;
  session_id?: string;
  target_external_key?: string;
  scheduleType: CronScheduleType;
  timezone: string;
  frequency: CronFrequency;
  time: Dayjs | null;
  minute: number;
  daysOfWeek: CronDay[];
  dayOfMonth: number;
  intervalValue: number;
  intervalUnit: CronIntervalUnit;
  cron: string;
  runAt: Dayjs | null;
  message: string;
}

function fieldLabel(label: string, description?: string) {
  return (
    <span className={styles.formLabel}>
      <span className={styles.formLabelText}>{label}</span>
      {description ? (
        <span className={styles.formLabelDescription} title={description}>
          （{description}）
        </span>
      ) : null}
    </span>
  );
}

function scheduleToForm(job: CronJob | null, draft: CronJobDraft | null): FormValues {
  const schedule = job?.schedule;
  const timezone = schedule?.timezone || 'Asia/Shanghai';
  const preset = parseCronPreset(schedule?.cron);
  return {
    name: job?.name || draft?.name || '',
    enabled: job?.enabled ?? true,
    datasource_id: job?.datasource_id || draft?.datasource_id || '',
    channel: job?.channel || draft?.channel || 'console',
    session_id: job?.session_id || draft?.session_id || undefined,
    target_external_key: job?.target_external_key || undefined,
    scheduleType: schedule?.type || 'cron',
    timezone,
    frequency: preset.frequency,
    time: dayjs(`2000-01-01T${preset.time}`),
    minute: preset.minute,
    daysOfWeek: preset.daysOfWeek,
    dayOfMonth: preset.dayOfMonth,
    intervalValue: preset.intervalValue,
    intervalUnit: preset.intervalUnit,
    cron: preset.cron,
    runAt: schedule?.run_at
      ? dayjs(isoToWallTimeInput(schedule.run_at, timezone))
      : dayjs().add(1, 'hour').second(0).millisecond(0),
    message: job?.message || draft?.message || '',
  };
}

export default function CronJobDrawer({
  open,
  editing,
  draft,
  datasources,
  submitting,
  onClose,
  onSubmit,
}: CronJobDrawerProps) {
  const { language, t } = useAppI18n();
  const [form] = Form.useForm<FormValues>();
  const [channelConfig, setChannelConfig] = useState<ChannelConfigResponse | null>(null);
  const [sessions, setSessions] = useState<ConsoleSession[]>([]);
  const [lookedUpSession, setLookedUpSession] = useState<ConsoleSession | null>(null);
  const [targets, setTargets] = useState<CronTarget[]>([]);
  const initialValues = useMemo(() => scheduleToForm(editing, draft), [draft, editing]);
  const scheduleType = Form.useWatch('scheduleType', form) ?? initialValues.scheduleType;
  const channel = Form.useWatch('channel', form) ?? initialValues.channel;
  const sessionId = Form.useWatch('session_id', form);
  const isIm = isCronImChannel(channel);
  const needsTarget = isIm;
  const lockedDatasourceId = lookedUpSession?.datasource_id || null;
  const datasourceLocked = Boolean(lockedDatasourceId);
  const frequency = Form.useWatch('frequency', form) ?? initialValues.frequency;
  const intervalUnit = Form.useWatch('intervalUnit', form) ?? initialValues.intervalUnit;
  const minuteOptions = useMemo(
    () =>
      Array.from({ length: 60 }, (_, minute) => ({
        value: minute,
        label: t('cron.minuteOption', { minute: String(minute).padStart(2, '0') }),
      })),
    [t],
  );
  const monthDayOptions = useMemo(
    () =>
      Array.from({ length: 31 }, (_, index) => ({
        value: index + 1,
        label: t('cron.monthDayOption', { day: index + 1 }),
      })),
    [t],
  );
  const cronDayOptions = useMemo(() => {
    const englishLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return CRON_DAYS.map((item, index) => ({
      ...item,
      label: language === 'zh' ? item.label : englishLabels[index],
    }));
  }, [language]);

  useEffect(() => {
    if (open) form.setFieldsValue(initialValues);
  }, [form, initialValues, open]);

  useEffect(() => {
    if (!lockedDatasourceId) return;
    form.setFieldValue('datasource_id', lockedDatasourceId);
  }, [form, lockedDatasourceId]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void channelConfigApi.get().then((config) => {
      if (!cancelled) setChannelConfig(config);
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open || !channel) return;
    let cancelled = false;
    void dataConsoleApi
      .listSessions({
        channel,
        page: 1,
        page_size: 100,
        sort: 'updated_desc',
      })
      .then((response) => {
        if (!cancelled) setSessions(response.items);
      });
    return () => {
      cancelled = true;
    };
  }, [channel, open]);

  useEffect(() => {
    const id = String(sessionId || '').trim();
    if (!open || !id) {
      setLookedUpSession(null);
      return;
    }
    const fromList = sessions.find((item) => item.id === id);
    if (fromList) {
      setLookedUpSession(fromList);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void dataConsoleApi
        .getSession(id)
        .then((response) => {
          if (!cancelled) setLookedUpSession(response.session);
        })
        .catch(() => {
          if (!cancelled) setLookedUpSession(null);
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [open, sessionId, sessions]);

  useEffect(() => {
    if (!open || !isIm) {
      setTargets([]);
      return;
    }
    let cancelled = false;
    void cronApi
      .listTargets(channel)
      .then((response) => {
        if (!cancelled) setTargets(response.targets);
      })
      .catch(() => {
        if (!cancelled) setTargets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [channel, isIm, open]);

  const channelOptions = useMemo(() => {
    const values = ['console', ...IM_CHANNELS];
    if (channel && !values.includes(channel)) values.push(channel);
    return values.map((value) => ({
      value,
      label: channelLabel(t, value),
    }));
  }, [channel, channelConfig, t]);

  const sessionOptions = useMemo(() => {
    const options = sessions.map((item) => ({
      value: item.id,
      label: `${item.title.trim() || t('common.unnamedSession')}（${item.id}）`,
    }));
    const current = editing?.session_id || draft?.session_id;
    if (current && !options.some((item) => item.value === current)) {
      options.unshift({ value: current, label: current });
    }
    return options;
  }, [draft?.session_id, editing?.session_id, sessions, t]);

  const targetOptions = useMemo(() => {
    const options = targets.map((item) => ({
      value: item.external_key,
      label: item.display_name ? `${item.display_name}（${item.external_key}）` : item.external_key,
    }));
    const current = editing?.target_external_key;
    if (current && !options.some((item) => item.value === current)) {
      options.unshift({ value: current, label: current });
    }
    return options;
  }, [editing?.target_external_key, targets]);

  const title = editing
    ? t('cron.editTitle')
    : draft
      ? t('cron.distillTitle')
      : t('cron.createTitle');

  const handleFinish = async (values: FormValues) => {
    try {
      const runAt = values.runAt
        ? wallTimeToIso(values.runAt.format('YYYY-MM-DDTHH:mm'), values.timezone)
        : null;
      if (values.scheduleType === 'once' && (!runAt || new Date(runAt).getTime() <= Date.now())) {
        throw new Error(t('cron.onceFutureRequired'));
      }
      const body: CronJobWriteBody = {
        name: values.name.trim(),
        enabled: values.enabled,
        message: values.message.trim(),
        datasource_id: (lockedDatasourceId || values.datasource_id).trim(),
        channel: values.channel,
        session_id: values.session_id?.trim() || null,
        target_external_key: isCronImChannel(values.channel)
          ? values.target_external_key?.trim() || null
          : null,
        schedule:
          values.scheduleType === 'once'
            ? {
                type: 'once',
                cron: null,
                run_at: runAt,
                timezone: values.timezone,
              }
            : {
                type: 'cron',
                cron: buildCronExpression({
                  frequency: values.frequency,
                  time: values.time?.format('HH:mm') || '',
                  custom: values.cron,
                  minute: values.minute,
                  daysOfWeek: values.daysOfWeek,
                  dayOfMonth: values.dayOfMonth,
                  intervalValue: values.intervalValue,
                  intervalUnit: values.intervalUnit,
                }),
                run_at: null,
                timezone: values.timezone,
              },
      };
      await onSubmit(body);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <RouteDrawer
      title={title}
      width={560}
      open={open}
      destroyOnHidden
      maskClosable={!submitting}
      onClose={submitting ? undefined : onClose}
      footer={
        <div className={styles.drawerFooter}>
          <Space>
            <Button disabled={submitting} onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="primary" loading={submitting} onClick={() => form.submit()}>
              {editing ? t('cron.saveChanges') : t('cron.create')}
            </Button>
          </Space>
        </div>
      }
    >
      {draft ? (
        <div className={styles.sourceBanner}>
          <strong>{t('cron.sourceBannerTitle')}</strong>
          <span>{t('cron.sourceBannerDescription')}</span>
        </div>
      ) : null}

      <Form
        className={styles.cronForm}
        form={form}
        layout="vertical"
        initialValues={initialValues}
        onFinish={(values) => void handleFinish(values)}
      >
        <Form.Item
          label={t('cron.name')}
          name="name"
          rules={[{ required: true, whitespace: true, message: t('cron.nameRequired') }]}
        >
          <Input placeholder={t('cron.namePlaceholder')} maxLength={80} />
        </Form.Item>

        <Form.Item
          label={fieldLabel(t('cron.channel'), t('cron.channelExtra'))}
          name="channel"
          rules={[{ required: true, message: t('cron.channelRequired') }]}
        >
          <Select
            options={channelOptions}
            onChange={() => {
              form.setFieldValue('session_id', undefined);
              form.setFieldValue('target_external_key', undefined);
            }}
          />
        </Form.Item>

        <Form.Item label={fieldLabel(t('cron.session'), t('cron.sessionExtra'))} name="session_id">
          <AutoComplete
            allowClear
            options={sessionOptions}
            placeholder={t('cron.sessionPlaceholder')}
            filterOption={(input, option) => {
              const label = String(option?.label ?? '');
              const value = String(option?.value ?? '');
              const needle = input.trim().toLowerCase();
              return label.toLowerCase().includes(needle) || value.toLowerCase().includes(needle);
            }}
          />
        </Form.Item>

        <Form.Item
          label={fieldLabel(
            t('cron.datasource'),
            datasourceLocked ? t('cron.datasourceLocked') : t('cron.datasourceExtra'),
          )}
          name="datasource_id"
          rules={[{ required: true, message: t('cron.datasourceRequired') }]}
        >
          <Select
            showSearch
            disabled={datasourceLocked}
            optionFilterProp="label"
            placeholder={datasources.length ? t('cron.selectDatasource') : t('cron.noDatasource')}
            options={datasources.map((item) => ({
              value: item.id,
              label: item.name,
            }))}
          />
        </Form.Item>

        {needsTarget ? (
          <Form.Item
            label={fieldLabel(
              t('cron.target'),
              targets.length ? t('cron.targetExtra') : t('cron.targetEmpty'),
            )}
            name="target_external_key"
            rules={[{ required: true, message: t('cron.targetRequired') }]}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              placeholder={t('cron.targetPlaceholder')}
              options={targetOptions}
            />
          </Form.Item>
        ) : null}

        <div className={styles.inlineFields}>
          <Form.Item label={t('cron.scheduleType')} name="scheduleType">
            <Select
              options={[
                { value: 'cron', label: t('cron.recurring') },
                { value: 'once', label: t('cron.once') },
              ]}
            />
          </Form.Item>
          <Form.Item label={t('cron.timezone')} name="timezone">
            <Select
              options={[
                { value: 'Asia/Shanghai', label: 'Asia/Shanghai' },
                { value: 'UTC', label: 'UTC' },
                { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
              ]}
            />
          </Form.Item>
        </div>

        {scheduleType === 'cron' ? (
          <>
            <Form.Item label={t('cron.frequency')} name="frequency">
              <Select
                options={[
                  { value: 'interval', label: t('cron.interval') },
                  { value: 'hourly', label: t('cron.hourly') },
                  { value: 'daily', label: t('cron.daily') },
                  { value: 'weekday', label: t('cron.weekday') },
                  { value: 'weekly', label: t('cron.weekly') },
                  { value: 'monthly', label: t('cron.monthly') },
                  { value: 'custom', label: t('cron.custom') },
                ]}
              />
            </Form.Item>
            {frequency === 'custom' ? (
              <Form.Item
                label={fieldLabel(t('cron.expression'), t('cron.expressionHint'))}
                name="cron"
                rules={[
                  { required: true, whitespace: true, message: t('cron.expressionRequired') },
                ]}
              >
                <Input placeholder="30 9 * * 1-5" />
              </Form.Item>
            ) : frequency === 'interval' ? (
              <Form.Item
                label={fieldLabel(t('cron.executionInterval'), t('cron.intervalHint'))}
                required
              >
                <Space.Compact block>
                  <Form.Item
                    name="intervalValue"
                    noStyle
                    rules={[{ required: true, message: t('cron.intervalRequired') }]}
                  >
                    <InputNumber
                      min={1}
                      max={intervalUnit === 'hour' ? 23 : 59}
                      precision={0}
                      className={styles.intervalValue}
                    />
                  </Form.Item>
                  <Form.Item name="intervalUnit" noStyle>
                    <Select
                      className={styles.intervalUnit}
                      options={[
                        { value: 'minute', label: t('cron.minute') },
                        { value: 'hour', label: t('cron.hour') },
                      ]}
                    />
                  </Form.Item>
                </Space.Compact>
              </Form.Item>
            ) : frequency === 'hourly' ? (
              <Form.Item
                label={t('cron.hourlyAt')}
                name="minute"
                rules={[{ required: true, message: t('cron.minuteRequired') }]}
              >
                <Select options={minuteOptions} />
              </Form.Item>
            ) : frequency === 'weekly' ? (
              <div className={styles.inlineFields}>
                <Form.Item
                  label={t('cron.weekdayLabel')}
                  name="daysOfWeek"
                  rules={[{ required: true, message: t('cron.weekdayRequired') }]}
                >
                  <Select mode="multiple" maxTagCount="responsive" options={cronDayOptions} />
                </Form.Item>
                <Form.Item
                  label={t('cron.executionTime')}
                  name="time"
                  rules={[{ required: true, message: t('cron.executionTimeRequired') }]}
                >
                  <TimePicker
                    format="HH:mm"
                    minuteStep={1}
                    needConfirm={false}
                    className={styles.fullWidthPicker}
                  />
                </Form.Item>
              </div>
            ) : frequency === 'monthly' ? (
              <div className={styles.inlineFields}>
                <Form.Item
                  label={t('cron.monthDay')}
                  name="dayOfMonth"
                  rules={[{ required: true, message: t('cron.monthDayRequired') }]}
                >
                  <Select options={monthDayOptions} />
                </Form.Item>
                <Form.Item
                  label={t('cron.executionTime')}
                  name="time"
                  rules={[{ required: true, message: t('cron.executionTimeRequired') }]}
                >
                  <TimePicker
                    format="HH:mm"
                    minuteStep={1}
                    needConfirm={false}
                    className={styles.fullWidthPicker}
                  />
                </Form.Item>
              </div>
            ) : (
              <Form.Item
                label={t('cron.executionTime')}
                name="time"
                rules={[{ required: true, message: t('cron.executionTimeRequired') }]}
              >
                <TimePicker
                  format="HH:mm"
                  minuteStep={1}
                  needConfirm={false}
                  className={styles.fullWidthPicker}
                />
              </Form.Item>
            )}
          </>
        ) : (
          <Form.Item
            label={t('cron.executionDateTime')}
            name="runAt"
            rules={[{ required: true, message: t('cron.executionTimeRequired') }]}
          >
            <DatePicker
              showTime={{ format: 'HH:mm' }}
              format="YYYY-MM-DD HH:mm"
              needConfirm={false}
              className={styles.fullWidthPicker}
            />
          </Form.Item>
        )}

        <Form.Item
          label={fieldLabel(t('cron.instruction'), t('cron.instructionExtra'))}
          name="message"
          rules={[{ required: true, whitespace: true, message: t('cron.instructionRequired') }]}
        >
          <Input.TextArea
            className={styles.largeTextArea}
            rows={7}
            maxLength={4000}
            showCount
            placeholder={t('cron.instructionPlaceholder')}
          />
        </Form.Item>

        <Form.Item label={t('cron.enableJob')} name="enabled" valuePropName="checked">
          <Switch checkedChildren={t('common.enable')} unCheckedChildren={t('common.disable')} />
        </Form.Item>
      </Form>
    </RouteDrawer>
  );
}
