import {
  queryBusinessDomainList,
  createBusinessDomain,
  updateBusinessDomain,
  deleteBusinessDomain,
} from '@/services/businessDomain';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import type {
  BusinessDomainCreateRequest,
  BusinessDomainItem,
  BusinessDomainQueryParams,
} from '@/types/businessDomain';
import { useBusinessDomainOptionsStore } from '@/store/businessDomainOptions';
import { isFormValidationError, toOptionalString } from '@/utils';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { Button, Drawer, message, Modal, Form, Input, Space } from '@/design';
import React, { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useHasCapability } from '@/store/authStore';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import { useSearchParams } from 'react-router-dom';

type BusinessDomainDeepLink = BusinessDomainQueryParams & { domain_id?: number };

const BusinessDomainManagement: React.FC = () => {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<BusinessDomainDeepLink>(searchParams, {
      domain_id: { parse: parseSemanticObjectId },
      domain_name: {},
    }),
  );
  const initialSearchValues: BusinessDomainQueryParams = {
    domain_name: deepLinkValues.domain_name,
  };
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const actionRef = useRef<ActionType | undefined>(undefined);
  const searchFormRef = useRef<ProFormInstance | undefined>(undefined);
  const [form] = Form.useForm();
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
  };

  // 打开编辑抽屉
  const handleOpenEditDrawer = (record: BusinessDomainItem) => {
    form.setFieldsValue({
      domain_name: record.domain_name,
      description: record.description,
    });
    setEditingRecord(record);
    setDrawerTitleKey('businessDomain.editTitle');
    setDrawerVisible(true);
  };

  // 抽屉确认
  const handleDrawerOk = async () => {
    try {
      const values = (await form.validateFields()) as BusinessDomainCreateRequest;
      setConfirmLoading(true);
      if (editingRecord) {
        await updateBusinessDomain(editingRecord.domain_id, {
          domain_name: values.domain_name,
          description: values.description,
        });
        message.success(t('common.editSuccess'));
      } else {
        // 新增模式
        await createBusinessDomain(values);
        message.success(t('common.createSuccess'));
      }
      setDrawerVisible(false);
      form.resetFields();
      useBusinessDomainOptionsStore.getState().invalidate();
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
          useBusinessDomainOptionsStore.getState().invalidate();
          actionRef.current?.reload();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: ProColumns<BusinessDomainItem>[] = [
    {
      width: 200,
      title: t('fields.businessDomainName'),
      ellipsis: true,
      dataIndex: 'domain_name',
      hideInSearch: false,
    },
    {
      title: t('fields.description'),
      dataIndex: 'description',
      hideInSearch: true,
      ellipsis: true,
      width: 230,
    },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      hideInSearch: true,
      fixed: 'right',
      width: 120,
      render: (_, record) => (
        <>
          {canWrite && (
            <Button type="link" onClick={() => handleOpenEditDrawer(record)} style={{ padding: 0 }}>
              {t('common.edit')}
            </Button>
          )}
          {canWrite && (
            <Button
              type="link"
              danger
              onClick={() => handleDelete(record)}
              style={{ padding: 0, marginLeft: 8 }}
            >
              {t('common.delete')}
            </Button>
          )}
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<BusinessDomainItem>
        rowKey="domain_id"
        actionRef={actionRef}
        formRef={searchFormRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        search={{
          labelWidth: 'auto',
        }}
        options={{ density: false }}
        scroll={{ x: 1150 }}
        request={async (params) => {
          const requestData: BusinessDomainQueryParams = {
            page: params.current,
            size: params.pageSize,
            domain_name: toOptionalString(params.domain_name),
          };
          if (deepLinkValues.domain_id) {
            const match = await loadSemanticDeepLinkInOrder([], () =>
              locateSemanticRecord({
                targetId: deepLinkValues.domain_id!,
                getRecordId: (record: BusinessDomainItem) => record.domain_id,
                size: params.pageSize,
                loadPage: (page, size) =>
                  queryBusinessDomainList({
                    page,
                    size,
                  }),
              }),
            );
            return {
              data: match ? [match.record] : [],
              success: true,
              total: match ? 1 : 0,
            };
          }
          const result = await queryBusinessDomainList(requestData);
          return {
            data: result.records,
            success: true,
            total: result.total,
          };
        }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
          pageSizeOptions: ['20', '50', '100'],
        }}
        toolBarRender={() => [
          ...(canExcel
            ? [
                <SemanticExcelActions
                  key="excel"
                  templateObject="biz_domain"
                  onImportSuccess={() => actionRef.current?.reload()}
                />,
              ]
            : []),
          ...(canWrite
            ? [
                <Button
                  key="add"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={handleOpenAddDrawer}
                >
                  {t('businessDomain.add')}
                </Button>,
              ]
            : []),
        ]}
      />

      <Drawer
        title={t(drawerTitleKey)}
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnHidden
        width={640}
        footer={
          <div style={{ textAlign: 'right' }}>
            <Space>
              <Button onClick={handleDrawerCancel}>{t('common.cancel')}</Button>
              <Button type="primary" loading={confirmLoading} onClick={() => void handleDrawerOk()}>
                {t('common.confirm')}
              </Button>
            </Space>
          </div>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="domain_name"
            label={t('fields.businessDomainName')}
            rules={[{ required: true, message: t('validation.inputBusinessDomainName') }]}
          >
            <Input placeholder={t('validation.inputBusinessDomainName')} allowClear />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('fields.description')}
            rules={[{ required: true, message: t('validation.inputDescription') }]}
          >
            <Input.TextArea placeholder={t('validation.inputDescription')} rows={3} />
          </Form.Item>
        </Form>
      </Drawer>
    </>
  );
};

export default BusinessDomainManagement;
