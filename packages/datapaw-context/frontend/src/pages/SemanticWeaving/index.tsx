import {
  confirmSemanticWeaving,
  killSemanticWeavingTask,
  querySemanticWeavingTasks,
} from '@/services/semanticWeaving';
import type { SemanticWeavingTask } from '@/types/semanticWeaving';
import { WEAVE_MODE_OPTIONS, TASK_STATUS_COLOR_MAP } from '@/constants';
import type { ActionType, ProColumns } from '@/design';
import { ProTable } from '@/design';
import { Button, Drawer, Form, Input, message, Modal, Select, Space, Tooltip, Typography } from '@/design';
import React, { useRef, useState } from 'react';
import { useDataSourceFilterOptions } from '@/hooks/useFilterOptions';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { formatDatasourceLabel, formatDateTime, isFormValidationError } from '@/utils';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;
const SemanticWeavingPage: React.FC = () => {
  const { t } = useTranslation();
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
      await confirmSemanticWeaving({
        datasource_id: values.datasource_id,
        task_name: values.task_name.trim(),
        weave_mode: values.weave_mode,
      });
      message.success(t('semanticWeaving.submitSuccess'));
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
      width: 140,
      valueType: 'text',
      fieldProps: {
        allowClear: false,
        placeholder: t('fields.dataSource'),
      },
      render: (_, record) => {
        if (!record.datasource_name) return '-';
        const text = formatDatasourceLabel(record.datasource_name, record.datasource_id);
        return (
          <Tooltip title={text}>
            <Text ellipsis style={{ maxWidth: '100%' }}>{text}</Text>
          </Tooltip>
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
      title: t('fields.weaveMode'),
      dataIndex: 'weave_mode',
      width: 100,
      hideInTable: true,
      hideInSearch: true,
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectWeaveMode') }] },
      fieldProps: {
        options: WEAVE_MODE_OPTIONS,
        placeholder: t('validation.selectWeaveMode'),
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
      render: (_, record) => (
        <span>
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              backgroundColor: TASK_STATUS_COLOR_MAP[record.status] || '#999',
              marginRight: 8,
            }}
          />
          {t(`semanticWeaving.status.${record.status}`, { defaultValue: record.status })}
        </span>
      ),
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
          {(record.status === 'RUNNING' || record.status === 'QUEUED') ? (
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
        footer={(
          <div style={{ textAlign: 'right' }}>
            <Space>
              <Button onClick={handleCloseDrawer}>{t('common.cancel')}</Button>
              <Button type="primary" loading={submitLoading} onClick={() => void handleSubmit()}>
                {t('common.submit')}
              </Button>
            </Space>
          </div>
        )}
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
          <Form.Item
            label={t('fields.weaveMode')}
            name="weave_mode"
            rules={[{ required: true, message: t('validation.selectWeaveMode') }]}
          >
            <Select placeholder={t('validation.selectWeaveMode')} options={WEAVE_MODE_OPTIONS} />
          </Form.Item>
        </Form>
      </Drawer>


      {/* 下半部分 - 任务列表 */}
        <ProTable<SemanticWeavingTask>
          rowKey="task_id"
          actionRef={actionRef}
          columns={columns}
          request={async (params) => {
            // ProTable 会把搜索表单的值合并进 params，需从中取出（排除分页字段）
            const { current, pageSize, ...searchValues } = params;
            try {
              const res = await querySemanticWeavingTasks({
                page: current,
                size: pageSize,
                ...searchValues,
              });
              const list = normalizeListResponse<SemanticWeavingTask>(res);
              const total = extractTotal(res) || list.length;
              return { data: list, success: true, total };
            } catch {
              return { data: [], success: false, total: 0 };
            }
          }}
          search={{
            labelWidth: 'auto',
          }}
           toolBarRender={() => [
           <Button type="primary" onClick={handleOpenDrawer}>
                {t('semanticWeaving.start')}
              </Button>
          ]}
          pagination={{
            defaultPageSize: 10,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50'],
          }}
        />
    </Space>
  );
};

export default SemanticWeavingPage;
