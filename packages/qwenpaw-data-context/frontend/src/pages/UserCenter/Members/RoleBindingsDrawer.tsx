import React, { useCallback, useEffect, useState } from 'react';
import { Button, Empty, Input, Modal, Select, Space, Tag, Tooltip, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { Drawer, Form } from '@/design';
import { authFetch } from '@/auth';
import type { RoleBinding, TenantRole, User, Workspace } from '@/auth';
import { useAuthStore } from '@/store/authStore';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';

type WorkspaceOption = Pick<Workspace, 'id' | 'name' | 'can_manage'>;

interface BindingFormValues {
  role: TenantRole;
  scope_type: 'tenant' | 'workspace';
  scope_id: string;
}

interface RoleBindingsDrawerProps {
  user: User | null;
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}

const ROLE_TAG_COLOR: Record<string, string> = {
  tenant_admin: '#f5222d',
  ws_admin: '#fa8c16',
  member: '#1890ff',
};

function bindingsFromUser(user: User): RoleBinding[] {
  return (user.role_bindings || []).map((binding) => ({
    ...binding,
    user_id: user.id,
    username: user.username,
    created_at: null,
  }));
}

const RoleBindingsDrawer: React.FC<RoleBindingsDrawerProps> = ({
  user,
  open,
  onClose,
  onChanged,
}) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [form] = Form.useForm<BindingFormValues>();
  const [bindings, setBindings] = useState<RoleBinding[]>([]);
  const [workspaceOptions, setWorkspaceOptions] = useState<WorkspaceOption[]>([]);
  const [adding, setAdding] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [loading, setLoading] = useState(false);

  const currentRole = useAuthStore((state) => state.activeWorkspaceRole);
  const activeWorkspaceId = useAuthStore((state) => state.activeWorkspaceId);
  const tenantId = useAuthStore((state) => state.tenantId);
  const isTenantAdmin = currentRole === 'tenant_admin';

  const loadBindings = useCallback(async (userId: string, fallback: RoleBinding[]) => {
    setLoading(true);
    try {
      const result = await authFetch<{ bindings: RoleBinding[] }>('/api/roles/bindings');
      setBindings((result.bindings || []).filter((binding) => binding.user_id === userId));
    } catch {
      setBindings(fallback);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadWorkspaces = useCallback(async () => {
    try {
      const result = await authFetch<{ workspaces: WorkspaceOption[] }>('/api/workspaces');
      setWorkspaceOptions((result.workspaces || []).filter((workspace) => workspace.can_manage));
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    if (!open || !user) {
      setAdding(false);
      form.resetFields();
      return;
    }
    const fallback = bindingsFromUser(user);
    setBindings(fallback);
    void loadBindings(user.id, fallback);
    void loadWorkspaces();
  }, [open, user, form, loadBindings, loadWorkspaces]);

  const refresh = useCallback(async () => {
    if (!user) return;
    const fallback = bindingsFromUser(user);
    await loadBindings(user.id, fallback);
    onChanged();
  }, [user, loadBindings, onChanged]);

  const handleOpenAdd = () => {
    form.resetFields();
    form.setFieldsValue({
      role: 'member',
      scope_type: 'workspace',
      scope_id: activeWorkspaceId || workspaceOptions[0]?.id,
    });
    setAdding(true);
  };

  const handleCancelAdd = () => {
    setAdding(false);
    form.resetFields();
  };

  const handleAdd = async () => {
    if (!user) return;
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);
      await authFetch('/api/roles/bindings', {
        method: 'POST',
        body: JSON.stringify({
          user_id: user.id,
          role: values.role,
          scope_type: values.scope_type,
          scope_id: values.scope_id,
        }),
      });
      message.success(t('common.createSuccess'));
      handleCancelAdd();
      await refresh();
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleDelete = (record: RoleBinding) => {
    if (record.can_delete === false) return;
    Modal.confirm({
      title: t('roleBinding.confirmDeleteTitle'),
      content: t('roleBinding.confirmDeleteContent', {
        username: user?.display_name || user?.username || record.username,
        role: t(`role.${record.role}`),
      }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await authFetch(`/api/roles/bindings/${record.id}`, { method: 'DELETE' });
          message.success(t('common.deleteSuccess'));
          await refresh();
        } catch (err: unknown) {
          message.error(getErrorMessage(err, t('common.operationFailed')));
        }
      },
    });
  };

  const getDenyReason = (record: RoleBinding) => {
    if (record.deny_reason === 'CANNOT_MODIFY_SELF') {
      return t('roleBinding.cannotModifySelf');
    }
    if (record.deny_reason === 'LAST_TENANT_ADMIN') {
      return t('roleBinding.lastTenantAdmin');
    }
    return t('roleBinding.operationUnavailable');
  };

  const selectedScopeType = Form.useWatch('scope_type', form);

  return (
    <Drawer
      title={t('roleBinding.manageTitle', { username: user?.display_name || user?.username || '' })}
      placement="right"
      open={open}
      onClose={onClose}
      destroyOnClose
      width={520}
    >
      {bindings.length === 0 && !loading ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('roleBinding.empty')}
          style={{ margin: '24px 0' }}
        />
      ) : (
        <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 16 }}>
          {bindings.map((binding) => (
            <div
              key={binding.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                padding: '8px 12px',
                border: '1px solid #f0f0f0',
                borderRadius: 8,
              }}
            >
              <Space size={8} wrap>
                <Tag color={ROLE_TAG_COLOR[binding.role] || '#999'} style={{ marginInlineEnd: 0 }}>
                  {t(`role.${binding.role}`)}
                </Tag>
                <span style={{ color: '#64748b', fontSize: 13 }}>
                  {binding.scope_name || t(`scopeType.${binding.scope_type}`)}
                </span>
              </Space>
              <Tooltip title={binding.can_delete === false ? getDenyReason(binding) : undefined}>
                <span>
                  <Button
                    type="link"
                    size="small"
                    danger
                    disabled={binding.can_delete === false}
                    onClick={() => handleDelete(binding)}
                    style={{ padding: 0 }}
                  >
                    {t('common.delete')}
                  </Button>
                </span>
              </Tooltip>
            </div>
          ))}
        </Space>
      )}

      {adding ? (
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item label={t('fields.user')}>
            <Input value={user?.display_name || user?.username || ''} disabled />
          </Form.Item>

          <Form.Item
            name="role"
            label={t('fields.role')}
            rules={[{ required: true, message: t('validation.roleRequired') }]}
          >
            <Select
              placeholder={t('validation.selectRole')}
              onChange={(role: TenantRole) => {
                const tenantScope = role === 'tenant_admin';
                form.setFieldsValue({
                  scope_type: tenantScope ? 'tenant' : 'workspace',
                  scope_id: tenantScope ? tenantId : activeWorkspaceId || workspaceOptions[0]?.id,
                });
              }}
            >
              {isTenantAdmin && (
                <Select.Option value="tenant_admin">{t('role.tenant_admin')}</Select.Option>
              )}
              {(isTenantAdmin || currentRole === 'ws_admin') && (
                <Select.Option value="ws_admin">{t('role.ws_admin')}</Select.Option>
              )}
              <Select.Option value="member">{t('role.member')}</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="scope_type"
            label={t('fields.scopeType')}
            rules={[{ required: true, message: t('validation.scopeTypeRequired') }]}
          >
            <Select placeholder={t('validation.selectScopeType')} disabled>
              {isTenantAdmin && (
                <Select.Option value="tenant">{t('scopeType.tenant')}</Select.Option>
              )}
              <Select.Option value="workspace">{t('scopeType.workspace')}</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="scope_id"
            label={t('fields.scope')}
            rules={[{ required: true, message: t('validation.scopeRequired') }]}
          >
            {selectedScopeType === 'workspace' ? (
              <Select
                placeholder={t('validation.selectWorkspace')}
                showSearch
                optionFilterProp="label"
                options={workspaceOptions.map((ws) => ({ label: ws.name, value: ws.id }))}
              />
            ) : (
              <Select placeholder={t('validation.selectScope')}>
                <Select.Option value={tenantId}>{tenantId}</Select.Option>
              </Select>
            )}
          </Form.Item>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button onClick={handleCancelAdd}>{t('common.cancel')}</Button>
            <Button type="primary" loading={confirmLoading} onClick={handleAdd}>
              {t('common.confirm')}
            </Button>
          </div>
        </Form>
      ) : (
        <Button type="dashed" block icon={<PlusOutlined />} onClick={handleOpenAdd}>
          {t('roleBinding.add')}
        </Button>
      )}
    </Drawer>
  );
};

export default RoleBindingsDrawer;
