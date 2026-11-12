import React, { useCallback, useRef, useState } from 'react';
import { Button, Input, Modal, Space, Switch, Tag, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { Drawer, Form, ProTable, type ActionType, type ProColumns } from '@/design';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import { normalizeProvisionBackends } from '@/auth';
import type { ProvisionBackend, Tenant } from '@/auth';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';
import { useProvisionStatusPoll } from '@/hooks/useProvisionStatusPoll';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  PROVISION_POLL_MAX_MS,
  isInstanceProvisioning,
  provisionStatusKey,
  shouldPollProvisioning,
} from '@/utils/provisionPoll';
import { useSearchParams } from 'react-router-dom';

type TenantItem = Tenant;

interface TenantFormValues {
  name: string;
  description?: string;
  join_open?: boolean;
  default_workspace_name?: string;
  tenant_admin_username?: string;
  tenant_admin_password?: string;
  endpoint_ref?: string;
  secret_ref?: string;
  instance_version?: string;
}

type DrawerMode = 'create' | 'edit' | 'resume';

const STATUS_COLOR: Record<string, string> = {
  active: '#52c41a',
  suspended: '#999',
  provisioning: '#1677ff',
  pending: '#faad14',
  queued: '#1677ff',
  ready: '#52c41a',
  failed: '#ff4d4f',
};

const TenantManagement: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<{
      name?: string;
    }>(searchParams, {
      name: { aliases: ['tenant_name', 'tenantName'] },
    }),
  );
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<TenantFormValues>();
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [editingRecord, setEditingRecord] = useState<TenantItem | null>(null);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>('create');
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [provisioningTenantId, setProvisioningTenantId] = useState<string | null>(null);
  const [provisionBackends, setProvisionBackends] = useState<ProvisionBackend[]>(() =>
    normalizeProvisionBackends(),
  );
  const provisionBackendsRef = useRef<ProvisionBackend[]>(provisionBackends);
  const [pollProvisioning, setPollProvisioning] = useState(false);
  const [password, setPassword] = useState('');
  const pollFailureCountRef = useRef(0);
  const pollStartedAtRef = useRef<number | null>(null);
  const lastStatusKeyRef = useRef('');

  const isEdit = drawerMode === 'edit';
  const isResume = drawerMode === 'resume';

  useProvisionStatusPoll({
    enabled: pollProvisioning,
    startedAtRef: pollStartedAtRef,
    lastKeyRef: lastStatusKeyRef,
    fetchItems: async () => {
      const result = await authFetch<{ tenants: TenantItem[] }>('/api/platform/tenants');
      return result.tenants || [];
    },
    onChanged: () => {
      actionRef.current?.reload();
    },
    onTimeout: () => {
      setPollProvisioning(false);
      message.warning(t('tenant.pollTimeout'));
    },
    onFailed: () => {
      setPollProvisioning(false);
      message.error(t('tenant.pollFailed'));
    },
    onIdle: () => {
      pollStartedAtRef.current = null;
      setPollProvisioning(false);
    },
  });

  const handleOpenAdd = () => {
    setDrawerMode('create');
    setEditingRecord(null);
    form.resetFields();
    setPassword('');
    setDrawerVisible(true);
  };

  const handleOpenEdit = (record: TenantItem) => {
    setDrawerMode('edit');
    setEditingRecord(record);
    form.resetFields();
    form.setFieldsValue({
      name: record.name,
      description: record.description,
      join_open: Boolean(record.join_open),
    });
    setDrawerVisible(true);
  };

  const handleOpenResume = (record: TenantItem) => {
    setDrawerMode('resume');
    setEditingRecord(record);
    form.resetFields();
    setDrawerVisible(true);
  };

  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    setEditingRecord(null);
    setDrawerMode('create');
    form.resetFields();
    setPassword('');
  };

  const handleDrawerOk = async () => {
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);

      if (isResume && editingRecord) {
        await authFetch(`/api/platform/tenants/${editingRecord.id}/resume`, {
          method: 'POST',
          body: JSON.stringify({
            endpoint_ref: values.endpoint_ref,
            secret_ref: values.secret_ref,
            instance_version: values.instance_version,
          }),
        });
        message.success(t('tenant.resumeSuccess'));
      } else if (isEdit && editingRecord) {
        await authFetch(`/api/platform/tenants/${editingRecord.id}`, {
          method: 'PUT',
          body: JSON.stringify({
            name: values.name,
            description: values.description,
            join_open: Boolean(values.join_open),
          }),
        });
        message.success(t('common.editSuccess'));
      } else {
        const result = await authFetch<{ onboarding_state: string }>(
          '/api/platform/tenants/onboard',
          {
            method: 'POST',
            body: JSON.stringify(values),
          },
        );
        if (result.onboarding_state === 'pending') {
          message.info(t('tenant.onboardingPending'));
        } else {
          message.success(t('common.createSuccess'));
        }
      }

      handleDrawerCancel();
      actionRef.current?.reload();
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleSuspend = useCallback(
    (record: TenantItem) => {
      Modal.confirm({
        title: t('tenant.confirmSuspendTitle'),
        content: t('tenant.confirmSuspendContent', { name: record.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await authFetch(`/api/platform/tenants/${record.id}/suspend`, { method: 'POST' });
            message.success(t('tenant.suspendSuccess'));
            actionRef.current?.reload();
          } catch (err: unknown) {
            message.error(getErrorMessage(err, t('common.operationFailed')));
          }
        },
      });
    },
    [t],
  );

  const handleProvision = useCallback(
    (record: TenantItem, backend: ProvisionBackend) => {
      const isHolo = backend === 'holo_age';
      Modal.confirm({
        title: isHolo ? t('tenant.confirmProvisionHoloTitle') : t('tenant.confirmProvisionTitle'),
        content: isHolo
          ? t('tenant.confirmProvisionHoloContent', { name: record.name })
          : t('tenant.confirmProvisionContent', { name: record.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        onOk: async () => {
          setProvisioningTenantId(record.id);
          try {
            await authFetch(`/api/platform/tenants/${record.id}/provision`, {
              method: 'POST',
              body: JSON.stringify({ backend }),
            });
            message.success(isHolo ? t('tenant.provisionHoloQueued') : t('tenant.provisionQueued'));
            pollStartedAtRef.current = Date.now();
            setPollProvisioning(true);
            actionRef.current?.reload();
          } catch (err: unknown) {
            message.error(getErrorMessage(err, t('common.operationFailed')));
          } finally {
            setProvisioningTenantId(null);
          }
        },
      });
    },
    [t],
  );

  const columns: ProColumns<TenantItem>[] = [
    {
      title: t('fields.tenantId'),
      dataIndex: 'id',
      width: 180,
      copyable: true,
      search: false,
    },
    {
      title: t('fields.name'),
      dataIndex: 'name',
      width: 180,
    },
    {
      title: t('fields.graphInstance'),
      dataIndex: 'neo4j_database',
      width: 220,
      search: false,
      render: (_, record) => {
        const isHolo =
          record.instance_version === 'holo_age' ||
          (record.endpoint_ref || '').startsWith('holo://');
        return (
          <Space size={4} wrap>
            <Tag color={isHolo ? '#722ed1' : '#1677ff'}>{isHolo ? 'Holo AGE' : 'Neo4j'}</Tag>
            <span>{isHolo ? record.endpoint_ref || '' : record.neo4j_database}</span>
          </Space>
        );
      },
    },
    {
      title: t('fields.status'),
      dataIndex: 'status',
      width: 100,
      search: false,
      render: (_, record) => (
        <Space size={[4, 4]} wrap>
          <Tag color={STATUS_COLOR[record.status] || '#999'}>{t(`status.${record.status}`)}</Tag>
          {record.onboarding_state && (
            <Tag color={STATUS_COLOR[record.onboarding_state] || '#999'}>
              {t('fields.onboardingStatus')}: {t(`status.${record.onboarding_state}`)}
            </Tag>
          )}
          {record.instance_status && (
            <Tag color={STATUS_COLOR[record.instance_status] || '#999'}>
              {t('fields.instanceStatus')}: {t(`status.${record.instance_status}`)}
            </Tag>
          )}
          {record.onboarding_error && <Tag color="error">{record.onboarding_error}</Tag>}
        </Space>
      ),
    },
    {
      title: t('fields.createdAt'),
      dataIndex: 'created_at',
      width: 180,
      search: false,
      valueType: 'dateTime',
    },
    {
      title: t('common.actions'),
      width: 160,
      search: false,
      fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button
            type="link"
            size="small"
            onClick={() => handleOpenEdit(record)}
            style={{ padding: 0 }}
          >
            {t('common.edit')}
          </Button>
          {record.id !== 'default' &&
            (record.instance_status === 'pending' || record.instance_status === 'failed') &&
            provisionBackendsRef.current.map((backend) => {
              const isHolo = backend === 'holo_age';
              const failed = record.instance_status === 'failed';
              return (
                <Button
                  key={backend}
                  type="link"
                  size="small"
                  loading={provisioningTenantId === record.id}
                  onClick={() => handleProvision(record, backend)}
                  style={{ padding: 0 }}
                >
                  {isHolo
                    ? failed
                      ? t('tenant.retryProvisionHolo')
                      : t('tenant.provisionHolo')
                    : failed
                      ? t('tenant.retryProvision')
                      : t('tenant.provision')}
                </Button>
              );
            })}
          {isInstanceProvisioning(record.instance_status) && (
            <Button type="link" size="small" loading disabled style={{ padding: 0 }}>
              {t('tenant.provisioning')}
            </Button>
          )}
          {record.status === 'active' && record.id !== 'default' && (
            <Button
              type="link"
              size="small"
              danger
              onClick={() => handleSuspend(record)}
              style={{ padding: 0 }}
            >
              {t('tenant.suspend')}
            </Button>
          )}
          {record.status === 'suspended' && record.id !== 'default' && (
            <Button
              type="link"
              size="small"
              onClick={() => handleOpenResume(record)}
              style={{ padding: 0 }}
            >
              {t('tenant.resume')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <ProTable<TenantItem>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        key={provisionBackends.join(',')}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          try {
            const result = await authFetch<{
              tenants: TenantItem[];
              provision_backends?: ProvisionBackend[];
            }>('/api/platform/tenants');
            const backends = normalizeProvisionBackends(result.provision_backends);
            provisionBackendsRef.current = backends;
            setProvisionBackends(backends);
            let list = result.tenants || [];
            lastStatusKeyRef.current = provisionStatusKey(list);
            const inFlight = shouldPollProvisioning(list);
            if (!inFlight) {
              pollStartedAtRef.current = null;
              setPollProvisioning(false);
            } else if (
              pollStartedAtRef.current != null &&
              Date.now() - pollStartedAtRef.current > PROVISION_POLL_MAX_MS
            ) {
              setPollProvisioning(false);
              message.warning(t('tenant.pollTimeout'));
            } else {
              if (pollStartedAtRef.current == null) {
                pollStartedAtRef.current = Date.now();
              }
              setPollProvisioning(true);
            }
            pollFailureCountRef.current = 0;
            const name = typeof params.name === 'string' ? params.name : '';
            if (name) {
              list = list.filter((item) => item.name.toLowerCase().includes(name.toLowerCase()));
            }
            return { data: list, success: true, total: list.length };
          } catch {
            if (pollProvisioning) {
              pollFailureCountRef.current += 1;
              if (pollFailureCountRef.current >= 3) {
                setPollProvisioning(false);
                message.error(t('tenant.pollFailed'));
              }
            }
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 900 }}
        options={{ density: false, reload: true }}
        search={{ labelWidth: 'auto' }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
        }}
        toolBarRender={() => [
          <Button key="add" type="primary" icon={<PlusOutlined />} onClick={handleOpenAdd}>
            {t('tenant.create')}
          </Button>,
        ]}
      />

      <Drawer
        title={
          isResume
            ? t('tenant.resumeTitle', { name: editingRecord?.name })
            : isEdit
              ? t('tenant.editTitle')
              : t('tenant.createTitle')
        }
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnClose
        width={520}
        footer={
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button onClick={handleDrawerCancel}>{t('common.cancel')}</Button>
            <Button type="primary" loading={confirmLoading} onClick={handleDrawerOk}>
              {t('common.confirm')}
            </Button>
          </div>
        }
      >
        <Form form={form} layout="vertical">
          {!isResume && (
            <>
              <Form.Item
                name="name"
                label={t('fields.name')}
                rules={[{ required: true, message: t('validation.nameRequired') }]}
              >
                <Input placeholder={t('validation.inputName')} allowClear />
              </Form.Item>

              {drawerMode === 'create' && (
                <>
                  <Form.Item
                    name="default_workspace_name"
                    label={t('fields.defaultWorkspace')}
                    rules={[{ required: true, message: t('validation.defaultWorkspaceRequired') }]}
                  >
                    <Input placeholder={t('validation.defaultWorkspacePlaceholder')} allowClear />
                  </Form.Item>
                  <Form.Item
                    name="tenant_admin_username"
                    label={t('fields.firstTenantAdmin')}
                    rules={[{ required: true, message: t('validation.tenantAdminRequired') }]}
                  >
                    <Input autoComplete="off" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="tenant_admin_password"
                    label={t('fields.initialPassword')}
                    rules={[
                      { required: true, message: t('validation.passwordRequired') },
                      { validator: createPasswordValidator((key) => t(key)) },
                    ]}
                  >
                    <Input.Password
                      autoComplete="new-password"
                      onChange={(event) => setPassword(event.target.value)}
                    />
                  </Form.Item>
                  <PasswordStrength password={password} />
                </>
              )}

              <Form.Item name="description" label={t('fields.description')}>
                <Input.TextArea
                  placeholder={t('validation.inputDescription')}
                  rows={4}
                  allowClear
                />
              </Form.Item>
              {isEdit && (
                <Form.Item
                  name="join_open"
                  label={t('tenant.joinOpen')}
                  valuePropName="checked"
                  extra={
                    <span style={{ color: '#ff4d4f', fontSize: 12 }}>
                      {t('tenant.joinOpenWarning')}
                    </span>
                  }
                >
                  <Switch />
                </Form.Item>
              )}
            </>
          )}

          {isResume && (
            <>
              <Form.Item
                name="endpoint_ref"
                label={t('fields.endpointRef')}
                rules={[{ required: true, message: t('validation.endpointRefRequired') }]}
              >
                <Input placeholder={t('validation.endpointRefPlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="secret_ref"
                label={t('fields.secretRef')}
                rules={[{ required: true, message: t('validation.secretRefRequired') }]}
              >
                <Input.Password
                  placeholder={t('validation.secretRefPlaceholder')}
                  autoComplete="off"
                />
              </Form.Item>
              <Form.Item
                name="instance_version"
                label={t('fields.instanceVersion')}
                rules={[{ required: true, message: t('validation.instanceVersionRequired') }]}
              >
                <Input placeholder={t('validation.instanceVersionPlaceholder')} allowClear />
              </Form.Item>
            </>
          )}
        </Form>
      </Drawer>
    </>
  );
};

export default TenantManagement;
