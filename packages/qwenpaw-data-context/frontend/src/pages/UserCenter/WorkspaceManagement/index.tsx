import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Space, Switch, Tag, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { Drawer, Form, ProTable, type ActionType, type ProColumns } from '@/design';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import { normalizeProvisionBackends } from '@/auth';
import type { ProvisionBackend, Workspace } from '@/auth';
import { useAuthStore } from '@/store/authStore';
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

type WorkspaceItem = Workspace;

interface WorkspaceManagementProps {
  /** Bump this counter to open the create drawer from outside (console quick action). */
  openCreateTick?: number;
}

interface WorkspaceFormValues {
  name: string;
  description?: string;
  join_open?: boolean;
}

const STATUS_COLOR: Record<string, string> = {
  active: '#52c41a',
  archived: '#999',
  suspended: '#999',
  provisioning: '#1677ff',
  pending: '#faad14',
  queued: '#1677ff',
  ready: '#52c41a',
  failed: '#ff4d4f',
};

const WorkspaceManagement: React.FC<WorkspaceManagementProps> = ({ openCreateTick }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<{
      name?: string;
    }>(searchParams, {
      name: { aliases: ['workspace_name', 'workspaceName'] },
    }),
  );
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<WorkspaceFormValues>();
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [editingRecord, setEditingRecord] = useState<WorkspaceItem | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [nameQuery, setNameQuery] = useState(initialSearchValues.name ?? '');
  const [provisioningWsId, setProvisioningWsId] = useState<string | null>(null);
  const [provisionBackends, setProvisionBackends] = useState<ProvisionBackend[]>(() =>
    normalizeProvisionBackends(),
  );
  const provisionBackendsRef = useRef<ProvisionBackend[]>(provisionBackends);
  const [pollProvisioning, setPollProvisioning] = useState(false);
  const pollFailureCountRef = useRef(0);
  const pollStartedAtRef = useRef<number | null>(null);
  const lastStatusKeyRef = useRef('');
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const isTenantAdmin = activeWorkspaceRole === 'tenant_admin';
  const canManageWorkspace = capabilities.includes('workspace:manage');

  useProvisionStatusPoll({
    enabled: pollProvisioning,
    startedAtRef: pollStartedAtRef,
    lastKeyRef: lastStatusKeyRef,
    fetchItems: async () => {
      const result = await authFetch<{ workspaces: WorkspaceItem[] }>('/api/workspaces');
      return result.workspaces || [];
    },
    onChanged: () => {
      actionRef.current?.reload();
    },
    onTimeout: () => {
      setPollProvisioning(false);
      message.warning(t('workspace.pollTimeout'));
    },
    onFailed: () => {
      setPollProvisioning(false);
      message.error(t('workspace.pollFailed'));
    },
    onIdle: () => {
      pollStartedAtRef.current = null;
      setPollProvisioning(false);
    },
  });

  const isEdit = Boolean(editingRecord);

  const handleOpenAdd = () => {
    setEditingRecord(null);
    form.resetFields();
    setDrawerVisible(true);
  };

  /* Console quick action: open the create drawer when the tick changes. */
  useEffect(() => {
    if (openCreateTick) {
      setEditingRecord(null);
      form.resetFields();
      setDrawerVisible(true);
    }
  }, [openCreateTick, form]);

  const handleOpenEdit = (record: WorkspaceItem) => {
    setEditingRecord(record);
    form.resetFields();
    form.setFieldsValue({
      name: record.name,
      description: record.description,
      join_open: Boolean(record.join_open),
    });
    setDrawerVisible(true);
  };

  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    setEditingRecord(null);
    form.resetFields();
  };

  const handleDrawerOk = async () => {
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);

      if (editingRecord) {
        await authFetch(`/api/workspaces/${editingRecord.id}`, {
          method: 'PUT',
          body: JSON.stringify(values),
        });
        message.success(t('common.editSuccess'));
      } else {
        const create = { ...values };
        delete create.join_open;
        await authFetch('/api/workspaces', {
          method: 'POST',
          body: JSON.stringify(create),
        });
        message.success(t('common.createSuccess'));
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

  const handleArchive = useCallback(
    (record: WorkspaceItem) => {
      Modal.confirm({
        title: t('workspace.confirmArchiveTitle'),
        content: t('workspace.confirmArchiveContent', { name: record.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await authFetch(`/api/workspaces/${record.id}`, { method: 'DELETE' });
            message.success(t('workspace.archiveSuccess'));
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
    (record: WorkspaceItem, backend: ProvisionBackend) => {
      const isHolo = backend === 'holo_age';
      Modal.confirm({
        title: isHolo
          ? t('workspace.confirmProvisionHoloTitle')
          : t('workspace.confirmProvisionTitle'),
        content: isHolo
          ? t('workspace.confirmProvisionHoloContent', { name: record.name })
          : t('workspace.confirmProvisionContent', { name: record.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        onOk: async () => {
          setProvisioningWsId(record.id);
          try {
            await authFetch(`/api/workspaces/${record.id}/provision`, {
              method: 'POST',
              body: JSON.stringify({ backend }),
            });
            message.success(
              isHolo ? t('workspace.provisionHoloQueued') : t('workspace.provisionQueued'),
            );
            pollStartedAtRef.current = Date.now();
            setPollProvisioning(true);
            actionRef.current?.reload();
          } catch (err: unknown) {
            message.error(getErrorMessage(err, t('common.operationFailed')));
          } finally {
            setProvisioningWsId(null);
          }
        },
      });
    },
    [t],
  );

  const columns: ProColumns<WorkspaceItem>[] = [
    {
      title: t('fields.name'),
      dataIndex: 'name',
      width: 200,
    },
    {
      title: t('fields.description'),
      dataIndex: 'description',
      width: 260,
      ellipsis: true,
      search: false,
      render: (_, record) => record.description || '-',
    },
    {
      title: t('fields.status'),
      dataIndex: 'status',
      width: 180,
      search: false,
      render: (_, record) => (
        <Space size={[4, 4]} wrap>
          <Tag color={STATUS_COLOR[record.status] || '#999'}>{t(`status.${record.status}`)}</Tag>
          {record.instance_status && (
            <Tag color={STATUS_COLOR[record.instance_status] || '#999'}>
              {t('fields.instanceStatus')}: {t(`status.${record.instance_status}`)}
            </Tag>
          )}
          {record.join_open ? <Tag color="#ff4d4f">{t('workspace.joinOpenTag')}</Tag> : null}
        </Space>
      ),
    },
    {
      title: t('fields.memberCount'),
      dataIndex: 'member_count',
      width: 120,
      search: false,
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
          {record.can_manage && (
            <Button
              type="link"
              size="small"
              onClick={() => handleOpenEdit(record)}
              style={{ padding: 0 }}
            >
              {t('common.edit')}
            </Button>
          )}
          {isTenantAdmin &&
            (record.instance_status === 'pending' || record.instance_status === 'failed') &&
            provisionBackendsRef.current.map((backend) => {
              const isHolo = backend === 'holo_age';
              const failed = record.instance_status === 'failed';
              return (
                <Button
                  key={backend}
                  type="link"
                  size="small"
                  loading={provisioningWsId === record.id}
                  onClick={() => handleProvision(record, backend)}
                  style={{ padding: 0 }}
                >
                  {isHolo
                    ? failed
                      ? t('workspace.retryProvisionHolo')
                      : t('workspace.provisionHolo')
                    : failed
                      ? t('workspace.retryProvision')
                      : t('workspace.provision')}
                </Button>
              );
            })}
          {isInstanceProvisioning(record.instance_status) && (
            <Button type="link" size="small" loading disabled style={{ padding: 0 }}>
              {t('workspace.provisioning')}
            </Button>
          )}
          {isTenantAdmin && record.status === 'active' && record.id !== 'default' && (
            <Button
              type="link"
              size="small"
              danger
              onClick={() => handleArchive(record)}
              style={{ padding: 0 }}
            >
              {t('workspace.archive')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <ProTable<WorkspaceItem>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        key={provisionBackends.join(',')}
        search={false}
        params={{ name: nameQuery }}
        headerTitle={
          <Input.Search
            allowClear
            placeholder={t('validation.inputName')}
            defaultValue={initialSearchValues.name}
            onSearch={setNameQuery}
            style={{ width: 240 }}
          />
        }
        request={async (params) => {
          try {
            const result = await authFetch<{
              workspaces: WorkspaceItem[];
              provision_backends?: ProvisionBackend[];
            }>('/api/workspaces');
            const backends = normalizeProvisionBackends(result.provision_backends);
            provisionBackendsRef.current = backends;
            setProvisionBackends(backends);
            let list = result.workspaces || [];
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
              message.warning(t('workspace.pollTimeout'));
            } else {
              if (pollStartedAtRef.current == null) {
                pollStartedAtRef.current = Date.now();
              }
              setPollProvisioning(true);
            }
            pollFailureCountRef.current = 0;
            const name = typeof params.name === 'string' ? params.name : '';
            if (name) {
              list = list.filter((w) => w.name.toLowerCase().includes(name.toLowerCase()));
            }
            return { data: list, success: true, total: list.length };
          } catch {
            if (pollProvisioning) {
              pollFailureCountRef.current += 1;
              if (pollFailureCountRef.current >= 3) {
                setPollProvisioning(false);
                message.error(t('workspace.pollFailed'));
              }
            }
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1000 }}
        options={{ density: false, reload: true }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
        }}
        toolBarRender={() =>
          isTenantAdmin && canManageWorkspace
            ? [
                <Button key="add" type="primary" icon={<PlusOutlined />} onClick={handleOpenAdd}>
                  {t('workspace.create')}
                </Button>,
              ]
            : []
        }
      />

      <Drawer
        title={isEdit ? t('workspace.editTitle') : t('workspace.createTitle')}
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
          <Form.Item
            name="name"
            label={t('fields.name')}
            rules={[{ required: true, message: t('validation.nameRequired') }]}
          >
            <Input placeholder={t('validation.inputName')} allowClear />
          </Form.Item>
          <Form.Item name="description" label={t('fields.description')}>
            <Input.TextArea placeholder={t('validation.inputDescription')} rows={4} allowClear />
          </Form.Item>
          {isEdit ? (
            <Form.Item
              name="join_open"
              label={t('workspace.joinOpen')}
              valuePropName="checked"
              extra={
                <span style={{ color: '#ff4d4f', fontSize: 12 }}>
                  {t('workspace.joinOpenWarning')}
                </span>
              }
            >
              <Switch />
            </Form.Item>
          ) : null}
        </Form>
      </Drawer>
    </>
  );
};

export default WorkspaceManagement;
