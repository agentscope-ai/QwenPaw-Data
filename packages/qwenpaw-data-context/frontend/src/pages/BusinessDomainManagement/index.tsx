import { queryBusinessDomainList, createBusinessDomain, updateBusinessDomain, deleteBusinessDomain } from '@/services/businessDomain';
import { BusinessDomainItem, BusinessDomainQueryParams } from '@/types/businessDomain';
import { useDataSourceFilterOptions } from '@/hooks/useFilterOptions';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { formatDatasourceLabel, isFormValidationError, omitKeys, toOptionalString } from '@/utils';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType } from '@/design';
import { Button, Drawer, message, Modal, Form, Input, Select, Space, Tooltip, Typography } from '@/design';
import React, { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

const BusinessDomainManagement: React.FC = () => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm();
  const { options: dataSourceOptions, refresh: refreshDataSource } = useDataSourceFilterOptions('full');
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [drawerTitleKey, setDrawerTitleKey] = useState('businessDomain.addTitle');
  const [editingRecord, setEditingRecord] = useState<BusinessDomainItem | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);

  // 打开新增抽屉
  const handleOpenAddDrawer = () => {
    form.resetFields();
    setEditingRecord(null);
    setDrawerTitleKey('businessDomain.addTitle');
    setDrawerVisible(true);
    // 打开抽屉时重新请求数据源数据
    refreshDataSource();
  };

  // 打开编辑抽屉
  const handleOpenEditDrawer = (record: BusinessDomainItem) => {
    // 打开抽屉时重新请求数据源数据
    refreshDataSource();
    form.setFieldsValue({
      datasource_id: record.datasource_id,
      domain_name: record.domain_name,
      display_name: record.display_name,
      description: record.description,
      aliases: record.aliases,
    });
    setEditingRecord(record);
    setDrawerTitleKey('businessDomain.editTitle');
    setDrawerVisible(true);
  };

  // 抽屉确认
  const handleDrawerOk = async () => {
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);
      if (editingRecord) {
        // 编辑模式：不传 datasource_id
        const rest = omitKeys(values, ['datasource_id']);
        await updateBusinessDomain(editingRecord.domain_id, rest);
        message.success(t('common.editSuccess'));
      } else {
        // 新增模式
        await createBusinessDomain(values);
        message.success(t('common.createSuccess'));
      }
      setDrawerVisible(false);
      form.resetFields();
      actionRef.current?.reload();
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
      // API 错误由响应拦截器统一提示
    } finally {
      setConfirmLoading(false);
    }
  };

  // 关闭抽屉
  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    form.resetFields();
    setEditingRecord(null);
  };

  // 删除业务域
  const handleDelete = (record: BusinessDomainItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: t('businessDomain.deleteTip'),
      onOk: async () => {
        try {
          await deleteBusinessDomain(record.domain_id);
          message.success(t('common.deleteSuccess'));
          actionRef.current?.reload();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: ProColumns<BusinessDomainItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
      hideInSearch: false,
      valueType: 'select',
      width: 200,
      fieldProps: {
        options: dataSourceOptions,
      },
      ellipsis: true,
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
      width: 200,
      title: t('fields.businessDomainName'),
      ellipsis: true,
      dataIndex: 'domain_name',
      hideInSearch: false,
    },
    {
       width: 200,
      title: t('fields.displayName'),
      ellipsis: true,
      dataIndex: 'display_name',
      hideInSearch: true,
    },
    {
      title: t('fields.description'),
      dataIndex: 'description',
      hideInSearch: true,
      ellipsis: true,
      width: 230,
    },
    {
       width: 200,
      title: t('fields.aliases'),
      dataIndex: 'aliases',
      hideInSearch: true,
      ellipsis: true,
    },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      hideInSearch: true,
      fixed: 'right',
      width: 120,
      render: (_, record) => (
        <>
          <Button type="link" onClick={() => handleOpenEditDrawer(record)} style={{ padding: 0 }}>
            {t('common.edit')}
          </Button>
          <Button type="link" danger onClick={() => handleDelete(record)} style={{ padding: 0, marginLeft: 8 }}>
            {t('common.delete')}
          </Button>
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<BusinessDomainItem>
        rowKey="domain_id"
        actionRef={actionRef}
        columns={columns}
        search={{
          labelWidth: 'auto',
        }}
        options={{ density: false }}
        scroll={{ x: 1150 }}
        request={async (params) => {
          const requestData: BusinessDomainQueryParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: toOptionalString(params.datasource_id),
            domain_name: toOptionalString(params.domain_name),
          };
          const result = await queryBusinessDomainList(requestData);
          const list = normalizeListResponse<BusinessDomainItem>(result);
          return {
            data: list,
            success: true,
            total: extractTotal(result) || list.length,
          };
        }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        toolBarRender={() => [
          <Button
            key="add"
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleOpenAddDrawer}
          >
              {t('businessDomain.add')}
          </Button>,
        ]}
      />

      <Drawer
        title={t(drawerTitleKey)}
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnHidden
        width={640}
        footer={(
          <div style={{ textAlign: 'right' }}>
            <Space>
              <Button onClick={handleDrawerCancel}>{t('common.cancel')}</Button>
              <Button type="primary" loading={confirmLoading} onClick={() => void handleDrawerOk()}>
                {t('common.confirm')}
              </Button>
            </Space>
          </div>
        )}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="datasource_id"
            label={t('fields.dataSource')}
            rules={[{ required: true, message: t('validation.selectDataSource') }]}
          >
            <Select
              placeholder={t('validation.selectDataSource')}
              options={dataSourceOptions}
              allowClear
              showSearch
              optionFilterProp="label"
              disabled={!!editingRecord}
            />
          </Form.Item>
          <Form.Item
            name="domain_name"
            label={t('fields.businessDomainName')}
            rules={[{ required: true, message: t('validation.inputBusinessDomainName') }]}
          >
            <Input placeholder={t('validation.inputBusinessDomainName')} allowClear />
          </Form.Item>
          <Form.Item name="display_name" label={t('fields.displayName')}>
            <Input placeholder={t('validation.inputDisplayName')} allowClear />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('fields.description')}
            rules={[{ required: true, message: t('validation.inputDescription') }]}
          >
            <Input.TextArea placeholder={t('validation.inputDescription')} rows={3} />
          </Form.Item>
          <Form.Item name="aliases" label={t('fields.aliases')}>
            <Input placeholder={t('businessDomain.aliasesPlaceholder')} allowClear />
          </Form.Item>
        </Form>
      </Drawer>
    </>
  );
};

export default BusinessDomainManagement;
