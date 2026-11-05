import { Button, Input, Select } from '@agentscope-ai/design';
import {
  DatePicker,
  Drawer,
  Form,
  InputNumber,
  Space,
  Switch,
  TimePicker,
  message,
} from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { useEffect, useMemo } from 'react';
import type {
  CronJob,
  CronJobDraft,
  CronJobWriteBody,
  CronScheduleType,
} from '@/features/console/api/cron';
import type { DatasourceOption } from '@/features/console/api/types';
import {
  buildCronExpression,
  CRON_DAYS,
  isoToWallTimeInput,
  parseCronPreset,
  wallTimeToIso,
  type CronDay,
  type CronFrequency,
  type CronIntervalUnit,
} from '@/features/console/model/cronSchedule';
import styles from './index.module.less';

interface CronJobDrawerProps {
  open: boolean;
  editing: CronJob | null;
  draft: CronJobDraft | null;
  datasources: DatasourceOption[];
  submitting: boolean;
  onClose: () => void;
  onSubmit: (body: CronJobWriteBody) => Promise<void>;
}

interface FormValues {
  name: string;
  enabled: boolean;
  datasource_id: string;
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

const MINUTE_OPTIONS = Array.from({ length: 60 }, (_, minute) => ({
  value: minute,
  label: `第 ${String(minute).padStart(2, '0')} 分`,
}));

const MONTH_DAY_OPTIONS = Array.from({ length: 31 }, (_, index) => ({
  value: index + 1,
  label: `${index + 1} 日`,
}));

function scheduleToForm(
  job: CronJob | null,
  draft: CronJobDraft | null,
): FormValues {
  const schedule = job?.schedule;
  const timezone = schedule?.timezone || 'Asia/Shanghai';
  const preset = parseCronPreset(schedule?.cron);
  return {
    name: job?.name || draft?.name || '',
    enabled: job?.enabled ?? true,
    datasource_id: job?.datasource_id || draft?.datasource_id || '',
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
  const [form] = Form.useForm<FormValues>();
  const initialValues = useMemo(
    () => scheduleToForm(editing, draft),
    [draft, editing],
  );
  const scheduleType = Form.useWatch('scheduleType', form)
    ?? initialValues.scheduleType;
  const frequency = Form.useWatch('frequency', form)
    ?? initialValues.frequency;
  const intervalUnit = Form.useWatch('intervalUnit', form)
    ?? initialValues.intervalUnit;

  useEffect(() => {
    if (open) form.setFieldsValue(initialValues);
  }, [form, initialValues, open]);

  const title = editing
    ? '编辑定时任务'
    : draft
      ? '提炼为定时任务'
      : '创建定时任务';

  const handleFinish = async (values: FormValues) => {
    try {
      const runAt = values.runAt
        ? wallTimeToIso(values.runAt.format('YYYY-MM-DDTHH:mm'), values.timezone)
        : null;
      if (values.scheduleType === 'once'
        && (!runAt || new Date(runAt).getTime() <= Date.now())) {
        throw new Error('单次任务执行时间必须晚于当前时间');
      }
      const body: CronJobWriteBody = {
        name: values.name.trim(),
        enabled: values.enabled,
        message: values.message.trim(),
        datasource_id: values.datasource_id.trim(),
        schedule: values.scheduleType === 'once'
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
    <Drawer
      title={title}
      width={560}
      open={open}
      destroyOnHidden
      maskClosable={!submitting}
      onClose={submitting ? undefined : onClose}
      footer={(
        <div className={styles.drawerFooter}>
          <Space>
            <Button disabled={submitting} onClick={onClose}>
              取消
            </Button>
            <Button
              type="primary"
              loading={submitting}
              onClick={() => form.submit()}
            >
              {editing ? '保存修改' : '创建任务'}
            </Button>
          </Space>
        </div>
      )}
    >
      {draft ? (
        <div className={styles.sourceBanner}>
          <strong>已从当前分析计划带入任务指令</strong>
          <span>数据源与执行步骤已预填，可修改后再创建。</span>
        </div>
      ) : null}

      <Form
        form={form}
        layout="vertical"
        initialValues={initialValues}
        onFinish={(values) => void handleFinish(values)}
      >
        <Form.Item
          label="任务名称"
          name="name"
          rules={[
            { required: true, whitespace: true, message: '请输入任务名称' },
          ]}
        >
          <Input placeholder="例如：每日指标诊断" maxLength={80} />
        </Form.Item>

        <Form.Item
          label="数据源"
          name="datasource_id"
          rules={[{ required: true, message: '请选择数据源' }]}
          extra="执行时会新建 Console 会话并绑定该数据源。"
        >
          <Select
            showSearch
            optionFilterProp="label"
            placeholder={datasources.length ? '选择数据源' : '暂无可用数据源'}
            options={datasources.map((item) => ({
              value: item.id,
              label: item.name,
            }))}
          />
        </Form.Item>

        <div className={styles.inlineFields}>
          <Form.Item label="计划类型" name="scheduleType">
            <Select
              options={[
                { value: 'cron', label: '循环任务' },
                { value: 'once', label: '单次任务' },
              ]}
            />
          </Form.Item>
          <Form.Item label="时区" name="timezone">
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
            <Form.Item label="执行频率" name="frequency">
              <Select
                options={[
                  { value: 'interval', label: '间隔' },
                  { value: 'hourly', label: '每小时' },
                  { value: 'daily', label: '每天' },
                  { value: 'weekday', label: '每个工作日' },
                  { value: 'weekly', label: '每周' },
                  { value: 'monthly', label: '每月' },
                  { value: 'custom', label: '自定义 Cron' },
                ]}
              />
            </Form.Item>
            {frequency === 'custom' ? (
              <Form.Item
                label="Cron 表达式"
                name="cron"
                rules={[{ required: true, whitespace: true, message: '请输入 Cron 表达式' }]}
                extra="5 段格式：分 时 日 月 周，例如 30 9 * * 1-5。"
              >
                <Input placeholder="30 9 * * 1-5" />
              </Form.Item>
            ) : frequency === 'interval' ? (
              <Form.Item label="执行间隔" required>
                <Space.Compact block>
                  <Form.Item
                    name="intervalValue"
                    noStyle
                    rules={[{ required: true, message: '请输入执行间隔' }]}
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
                        { value: 'minute', label: '分钟' },
                        { value: 'hour', label: '小时' },
                      ]}
                    />
                  </Form.Item>
                </Space.Compact>
                <div className={styles.fieldHint}>小时最多间隔 23，分钟最多间隔 59。</div>
              </Form.Item>
            ) : frequency === 'hourly' ? (
              <Form.Item
                label="每小时执行"
                name="minute"
                rules={[{ required: true, message: '请选择执行分钟' }]}
              >
                <Select options={MINUTE_OPTIONS} />
              </Form.Item>
            ) : frequency === 'weekly' ? (
              <div className={styles.inlineFields}>
                <Form.Item
                  label="星期"
                  name="daysOfWeek"
                  rules={[{ required: true, message: '请至少选择一个星期' }]}
                >
                  <Select
                    mode="multiple"
                    maxTagCount="responsive"
                    options={[...CRON_DAYS]}
                  />
                </Form.Item>
                <Form.Item
                  label="执行时间"
                  name="time"
                  rules={[{ required: true, message: '请选择执行时间' }]}
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
                  label="每月日期"
                  name="dayOfMonth"
                  rules={[{ required: true, message: '请选择每月日期' }]}
                >
                  <Select options={MONTH_DAY_OPTIONS} />
                </Form.Item>
                <Form.Item
                  label="执行时间"
                  name="time"
                  rules={[{ required: true, message: '请选择执行时间' }]}
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
                label="执行时间"
                name="time"
                rules={[{ required: true, message: '请选择执行时间' }]}
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
            label="执行日期与时间"
            name="runAt"
            rules={[{ required: true, message: '请选择执行时间' }]}
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
          label="任务指令"
          name="message"
          rules={[
            { required: true, whitespace: true, message: '请输入任务指令' },
          ]}
          extra="到点后 Host 会将该指令写入新 Chat 并启动 Agent。"
        >
          <Input.TextArea
            rows={7}
            maxLength={4000}
            showCount
            placeholder="描述每次运行需要完成的分析工作"
          />
        </Form.Item>

        <Form.Item label="启用任务" name="enabled" valuePropName="checked">
          <Switch checkedChildren="启用" unCheckedChildren="停用" />
        </Form.Item>
      </Form>
    </Drawer>
  );
}
