import {
  confirmSemanticWeaving,
  killSemanticWeavingTask,
  querySemanticWeavingTasks,
} from '@/services/semanticWeaving';
import type { SemanticWeavingTask, SemanticWeavingTaskQueryParams } from '@/types/semanticWeaving';
import { TASK_STATUS_COLOR_MAP } from '@/constants';
import type { ActionType, ProColumns } from '@/design';
import { ProTable } from '@/design';
import {
  Button,
  Drawer,
  Form,
  Input,
  message,
  Modal,
  Select,
  Space,
  Tooltip,
  Typography,
} from '@/design';
import React, { useRef, useState } from 'react';
import { useDataSourceFilterOptions } from '@/hooks/useFilterOptions';
import { formatDateTime, isFormValidationError } from '@/utils';
import { useTranslation } from 'react-i18next';
import { readUrlSearchValues } from '@/utils/urlSearch';
import { useSearchParams } from 'react-router-dom';
import {
  SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE,
  SEMANTIC_WEAVING_POLL_INTERVAL_MS,
  SEMANTIC_WEAVING_PROTOCOL_ERROR_COLOR,
  buildSemanticWeavingSubmitParams,
  buildSemanticWeavingTaskQuery,
  canKillSemanticWeavingTask,
  getSemanticWeavingStatusView,
  getSemanticWeavingSubmitFeedback,
} from './model';

const { Text } = Typography;
const SemanticWeavingPage: React.FC = () => {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<SemanticWeavingTaskQueryParams>(searchParams, {
      datasource_name: {},
      task_name: {},
    }),
  );
  const [submitForm] = Form.useForm();
  const [submitLoading, setSubmitLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { options: dataSourceOptions, refresh: refreshDataSource } = useDataSourceFilterOptions();
  const actionRef = useRef<ActionType | undefined>(undefined);

  // 打开任务发起抽屉
  const handleOpenDrawer = () => {
    setDrawerOpen(true);
    // 打开抽屉时重新请求数据源数据
    refreshDataSource();
  };

  const handleCloseDrawer = () => {
    setDrawerOpen(false);
    submitForm.resetFields();
  };

  // 抽屉提交任务
  const handleSubmit = async () => {
    try {
      const values = await submitForm.validateFields();
      setSubmitLoading(true);
      const task = await confirmSemanticWeaving(buildSemanticWeavingSubmitParams(values));
      const feedback = getSemanticWeavingSubmitFeedback(task.status);
      const feedbackText = t(feedback.translationKey, {
        message: task.error_msg || t('common.failed'),
        status: task.status,
      });
      if (feedback.kind === 'error') message.error(feedbackText);
      else if (feedback.kind === 'warning') message.warning(feedbackText);
      else message.success(feedbackText);
      submitForm.resetFields();
      setDrawerOpen(false);
      actionRef.current?.reload();
    } catch (err: unknown) {
      if (isFormValidationError(err)) {
        return;
      }
      // API 错误由响应拦截器统一提示
    } finally {
      setSubmitLoading(false);
    }
  };

  // 杀死任务
  const handleKillTask = (taskId: string) => {
    Modal.confirm({
      title: t('semanticWeaving.confirmOperation'),
      content: t('semanticWeaving.confirmKill', { taskId }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await killSemanticWeavingTask(taskId);
          message.success(t('semanticWeaving.killSuccess'));
          actionRef.current?.reload();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: ProColumns<SemanticWeavingTask>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_name',
      width: 220,
      fixed: 'left',
      valueType: 'text',
      fieldProps: {
        allowClear: false,
        placeholder: t('fields.dataSource'),
      },
      render: (_, record) => {
        const datasourceName = record.datasource_name?.trim() || '-';
        const datasourceId = record.datasource_id?.trim();
        return (
          <Space direction="vertical" size={0} style={{ width: '100%', lineHeight: 1.4 }}>
            <Tooltip title={datasourceName}>
              <Text ellipsis style={{ display: 'block', maxWidth: 190 }}>
                {datasourceName}
              </Text>
            </Tooltip>
            {datasourceId ? (
              <Tooltip title={datasourceId}>
                <Text
                  type="secondary"
                  ellipsis
                  style={{ display: 'block', maxWidth: 190, fontSize: 12 }}
                >
                  {datasourceId}
                </Text>
              </Tooltip>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: t('fields.taskName'),
      dataIndex: 'task_name',
      width: 180,
      ellipsis: true,
      fieldProps: {
        placeholder: t('validation.inputTaskName'),
      },
    },

    {
      title: t('fields.taskId'),
      dataIndex: 'task_id',
      width: 240,
      ellipsis: true,
      hideInSearch: true,
    },
    {
      title: t('fields.taskStatus'),
      dataIndex: 'status',
      width: 140,
      hideInSearch: true,
      render: (_, record) => {
        const statusView = getSemanticWeavingStatusView(record.status);
        const color =
          statusView.kind === 'known'
            ? TASK_STATUS_COLOR_MAP[statusView.status]
            : SEMANTIC_WEAVING_PROTOCOL_ERROR_COLOR;
        const label =
          statusView.kind === 'known'
            ? t(statusView.translationKey)
            : t(statusView.translationKey, { status: statusView.rawStatus });
        return (
          <span>
            <span
              style={{
                display: 'inline-block',
                width: 8,
                height: 8,
                borderRadius: '50%',
                backgroundColor: color,
                marginRight: 8,
              }}
            />
            {label}
          </span>
        );
      },
    },
    {
      title: t('fields.time'),
      dataIndex: 'created_at',
      width: 160,
      ellipsis: true,
      hideInSearch: true,
      render: (_, record) => formatDateTime(record.created_at),
    },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      width: 160,
      hideInSearch: true,
      align: 'center',
      render: (_, record) => (
        <Space size="small">
          {canKillSemanticWeavingTask(record.status) ? (
            <Button danger size="small" onClick={() => handleKillTask(record.task_id)}>
              {t('semanticWeaving.kill')}
            </Button>
          ) : (
            <>{'-'}</>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {/* 任务发起抽屉 */}
      <Drawer
        title={t('semanticWeaving.startTask')}
        placement="right"
        open={drawerOpen}
        onClose={handleCloseDrawer}
        destroyOnHidden
        width={640}
        footer={
          <div style={{ textAlign: 'right' }}>
            <Space>
              <Button onClick={handleCloseDrawer}>{t('common.cancel')}</Button>
              <Button type="primary" loading={submitLoading} onClick={() => void handleSubmit()}>
                {t('common.submit')}
              </Button>
            </Space>
          </div>
        }
      >
        <Form form={submitForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            label={t('fields.dataSource')}
            name="datasource_id"
            rules={[{ required: true, message: t('validation.selectDataSource') }]}
          >
            <Select
              placeholder={t('validation.selectDataSource')}
              options={dataSourceOptions}
              allowClear
              showSearch
              optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item
            label={t('fields.taskName')}
            name="task_name"
            rules={[{ required: true, whitespace: true, message: t('validation.inputTaskName') }]}
          >
            <Input placeholder={t('validation.inputTaskName')} allowClear />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 下半部分 - 任务列表 */}
      <ProTable<SemanticWeavingTask>
        rowKey="task_id"
        actionRef={actionRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          // ProTable 会把搜索表单的值合并进 params，需从中取出（排除分页字段）
          try {
            const res = await querySemanticWeavingTasks(buildSemanticWeavingTaskQuery(params));
            return { data: res.records, success: true, total: res.total };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        search={{
          labelWidth: 'auto',
        }}
        polling={SEMANTIC_WEAVING_POLL_INTERVAL_MS}
        scroll={{ x: 1160 }}
        toolBarRender={() => [
          <Button key="start" type="primary" onClick={handleOpenDrawer}>
            {t('semanticWeaving.start')}
          </Button>,
        ]}
        pagination={{
          defaultPageSize: SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
        }}
      />
    </Space>
  );
};

export default SemanticWeavingPage;
