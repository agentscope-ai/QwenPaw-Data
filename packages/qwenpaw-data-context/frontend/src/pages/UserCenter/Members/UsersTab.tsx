import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Select, Space, Tag, Tooltip, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { Drawer, Form, ProTable, type ActionType, type ProColumns } from '@/design';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import type { TenantRole, User, Workspace } from '@/auth';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import RoleBindingsDrawer from '@/pages/UserCenter/Members/RoleBindingsDrawer';
import { useAuthStore } from '@/store/authStore';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';
import { readUrlSearchValues } from '@/utils/urlSearch';
import { useSearchParams } from 'react-router-dom';

/* ---------- types ---------- */

type UserItem = User;
type WorkspaceOption = Pick<Workspace, 'id' | 'name' | 'can_manage'>;

interface InviteFormValues {
  username: string;
  password: string;
  role: TenantRole;
  workspace_id?: string;
}

interface UsersTabProps {
  /** Bump this counter to open the invite drawer from outside (console quick action). */
  openInviteTick?: number;
}

const ROLE_TAG_COLOR: Record<string, string> = {
  tenant_admin: '#f5222d',
  ws_admin: '#fa8c16',
  member: '#1890ff',
};

const STATUS_COLOR: Record<string, string> = {
  pending: '#faad14',
  active: '#52c41a',
  disabled: '#999',
};

/* ---------- component ---------- */

const UsersTab: React.FC<UsersTabProps> = ({ openInviteTick }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<{
      username?: string;
    }>(searchParams, {
      username: {},
    }),
  );
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<InviteFormValues>();
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [drawerMode, setDrawerMode] = useState<'invite' | 'resetPassword'>('invite');
  const [selectedUser, setSelectedUser] = useState<UserItem | null>(null);
  const [bindingUser, setBindingUser] = useState<UserItem | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [password, setPassword] = useState('');
  const [usernameQuery, setUsernameQuery] = useState(initialSearchValues.username ?? '');
  const [workspaceOptions, setWorkspaceOptions] = useState<WorkspaceOption[]>([]);
  const currentUserId = useAuthStore((state) => state.userId);
  const activeWorkspaceId = useAuthStore((state) => state.activeWorkspaceId);
  const currentRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const isTenantAdmin = currentRole === 'tenant_admin';
  const canManageUsers = capabilities.includes('user:manage');

  const loadWorkspaces = useCallback(async () => {
    try {
      const result = await authFetch<{ workspaces: WorkspaceOption[] }>('/api/workspaces');
      setWorkspaceOptions((result.workspaces || []).filter((workspace) => workspace.can_manage));
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    loadWorkspaces();
  }, [loadWorkspaces]);

  /* ---- drawer open/close ---- */

  const handleOpenInvite = () => {
    setDrawerMode('invite');
    setSelectedUser(null);
    form.resetFields();
    form.setFieldsValue({
      role: 'member',
      workspace_id: activeWorkspaceId || workspaceOptions[0]?.id,
    });
    setPassword('');
    setDrawerVisible(true);
  };

  /* Console quick action: open the invite drawer when the tick changes. */
  const openInviteRef = useRef(handleOpenInvite);
  openInviteRef.current = handleOpenInvite;
  useEffect(() => {
    if (openInviteTick) openInviteRef.current();
  }, [openInviteTick]);

  const handleOpenResetPassword = (record: UserItem) => {
    setDrawerMode('resetPassword');
    setSelectedUser(record);
    form.resetFields();
    setPassword('');
    setDrawerVisible(true);
  };

  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    setSelectedUser(null);
    form.resetFields();
    setPassword('');
  };

  /* ---- submit handlers ---- */

  const handleInviteSubmit = async () => {
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);

      await authFetch<{ user: UserItem }>('/api/users', {
        method: 'POST',
        body: JSON.stringify({
          username: values.username,
          password: values.password,
          role: values.role,
          workspace_id: values.role === 'tenant_admin' ? undefined : values.workspace_id,
        }),
      });

      message.success(t('user.inviteSuccess'));
      handleDrawerCancel();
      actionRef.current?.reload();
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleResetPasswordSubmit = async () => {
    if (!selectedUser) return;
    try {
      const values = await form.validateFields();
      setConfirmLoading(true);
      await authFetch(`/api/users/${selectedUser.id}`, {
        method: 'PUT',
        body: JSON.stringify({ password: values.password }),
      });
      message.success(t('user.resetPasswordSuccess'));
      handleDrawerCancel();
      actionRef.current?.reload();
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setConfirmLoading(false);
    }
  };

  /* ---- enable / disable ---- */

  const handleToggleStatus = useCallback(
    (record: UserItem) => {
      if (record.can_disable !== true) return;
      const isActive = record.status === 'active';
      Modal.confirm({
        title: isActive ? t('user.confirmDisableTitle') : t('user.confirmEnableTitle'),
        content: isActive
          ? t('user.confirmDisableContent', { name: record.display_name || record.username })
          : t('user.confirmEnableContent', { name: record.display_name || record.username }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: isActive ? { danger: true } : undefined,
        onOk: async () => {
          try {
            const action = isActive ? 'disable' : 'enable';
            await authFetch(`/api/users/${record.id}/${action}`, { method: 'POST' });
            message.success(isActive ? t('user.disableSuccess') : t('user.enableSuccess'));
            actionRef.current?.reload();
          } catch (err: unknown) {
            message.error(getErrorMessage(err, t('common.operationFailed')));
          }
        },
      });
    },
    [t],
  );

  const getDenyReason = useCallback(
    (record: UserItem) => {
      if (record.deny_reason === 'CANNOT_MANAGE_SELF') {
        return t('user.cannotManageSelf');
      }
      if (record.deny_reason === 'LAST_TENANT_ADMIN') {
        return t('user.lastTenantAdmin');
      }
      return t('user.operationUnavailable');
    },
    [t],
  );

  const canManageBindings = useCallback(
    (record: UserItem) => canManageUsers && record.id !== currentUserId,
    [canManageUsers, currentUserId],
  );

  const getBindingsDenyReason = useCallback(
    (record: UserItem) => {
      if (!canManageUsers) return t('user.operationUnavailable');
      if (record.id === currentUserId) return t('roleBinding.cannotModifySelf');
      return undefined;
    },
    [canManageUsers, currentUserId, t],
  );

  const handleOpenBindings = useCallback(
    (record: UserItem) => {
      if (!canManageUsers || record.id === currentUserId) return;
      setBindingUser(record);
    },
    [canManageUsers, currentUserId],
  );

  /* ---- columns ---- */

  const columns: ProColumns<UserItem>[] = [
    {
      title: t('fields.username'),
      dataIndex: 'username',
      width: 160,
      // Show the display name with the username as a secondary line when they differ.
      render: (_, record) => {
        const display = record.display_name || record.username;
        if (display === record.username) return display;
        return (
          <div>
            <div>{display}</div>
            <div style={{ fontSize: 12, color: '#94a3b8' }}>{record.username}</div>
          </div>
        );
      },
    },
    {
      title: t('fields.roleBindings'),
      dataIndex: 'role_bindings',
      width: 280,
      search: false,
      render: (_, record) => {
        const clickable = canManageBindings(record);
        const tagStyle = clickable ? { cursor: 'pointer' } : undefined;
        const openBindings = clickable ? () => handleOpenBindings(record) : undefined;
        if (!record.role_bindings || record.role_bindings.length === 0) {
          return (
            <Tag style={tagStyle} onClick={openBindings}>
              {t('role.none')}
            </Tag>
          );
        }
        return (
          <Space size={[4, 4]} wrap>
            {record.role_bindings.map((rb) => (
              <Tag
                key={rb.id}
                color={ROLE_TAG_COLOR[rb.role] || '#999'}
                style={tagStyle}
                onClick={openBindings}
              >
                {t(`role.${rb.role}`)}
                {rb.scope_name ? ` @ ${rb.scope_name}` : ''}
              </Tag>
            ))}
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
        <Tag color={STATUS_COLOR[record.status] || '#999'}>{t(`status.${record.status}`)}</Tag>
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
      width: 280,
      search: false,
      fixed: 'right',
      render: (_, record) => {
        const canToggleStatus = record.can_disable === true;
        const canResetPassword = record.id !== currentUserId;
        const bindingsAllowed = canManageBindings(record);
        return (
          <Space>
            <Tooltip title={bindingsAllowed ? undefined : getBindingsDenyReason(record)}>
              <span>
                <Button
                  type="link"
                  size="small"
                  disabled={!bindingsAllowed}
                  onClick={() => handleOpenBindings(record)}
                  style={{ padding: 0 }}
                >
                  {t('roleBinding.manage')}
                </Button>
              </span>
            </Tooltip>
            <Tooltip title={canToggleStatus ? undefined : getDenyReason(record)}>
              <span>
                <Button
                  type="link"
                  size="small"
                  disabled={!canToggleStatus}
                  onClick={() => handleToggleStatus(record)}
                  style={{ padding: 0 }}
                >
                  {record.status === 'active' ? t('user.disable') : t('user.enable')}
                </Button>
              </span>
            </Tooltip>
            <Tooltip title={canResetPassword ? undefined : t('user.changeOwnPasswordHint')}>
              <span>
                <Button
                  type="link"
                  size="small"
                  disabled={!canResetPassword}
                  onClick={() => handleOpenResetPassword(record)}
                  style={{ padding: 0 }}
                >
                  {t('user.resetPassword')}
                </Button>
              </span>
            </Tooltip>
          </Space>
        );
      },
    },
  ];

  const selectedRole = Form.useWatch('role', form);

  /* ---- render ---- */

  return (
    <>
      <ProTable<UserItem>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        params={{ username: usernameQuery }}
        request={async (params) => {
          try {
            const result = await authFetch<{ users: UserItem[] }>('/api/users');
            let list = result.users || [];
            const username = typeof params.username === 'string' ? params.username : '';
            if (username) {
              list = list.filter((u) => u.username.toLowerCase().includes(username.toLowerCase()));
            }
            return { data: list, success: true, total: list.length };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1100 }}
        options={{ density: false, reload: true }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
        }}
        toolBarRender={() => [
          <Input.Search
            key="username-search"
            allowClear
            placeholder={t('validation.inputUsername')}
            defaultValue={initialSearchValues.username}
            onSearch={setUsernameQuery}
            style={{ width: 240 }}
          />,
          ...(canManageUsers
            ? [
                <Button
                  key="invite"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={handleOpenInvite}
                >
                  {t('user.invite')}
                </Button>,
              ]
            : []),
        ]}
      />

      <Drawer
        title={drawerMode === 'invite' ? t('user.inviteTitle') : t('user.resetPasswordTitle')}
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnClose
        width={520}
        footer={
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button onClick={handleDrawerCancel}>{t('common.cancel')}</Button>
            <Button
              type="primary"
              loading={confirmLoading}
              onClick={drawerMode === 'invite' ? handleInviteSubmit : handleResetPasswordSubmit}
            >
              {t('common.confirm')}
            </Button>
          </div>
        }
      >
        <Form form={form} layout="vertical">
          {drawerMode === 'invite' && (
            <>
              <Form.Item
                name="username"
                label={t('fields.username')}
                rules={[
                  { required: true, message: t('validation.usernameRequired') },
                  { min: 2, max: 32, message: t('validation.usernameLength') },
                  { pattern: /^[a-zA-Z0-9_-]+$/, message: t('validation.usernamePattern') },
                ]}
              >
                <Input placeholder={t('validation.inputUsername')} allowClear />
              </Form.Item>

              <Form.Item
                name="password"
                label={t('fields.password')}
                rules={[
                  { required: true, message: t('validation.passwordRequired') },
                  { validator: createPasswordValidator((key) => t(key)) },
                ]}
              >
                <Input.Password
                  placeholder={t('validation.inputPassword')}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Form.Item>

              <PasswordStrength password={password} />

              <Form.Item
                name="role"
                label={t('fields.role')}
                style={{ marginTop: 16 }}
                rules={[{ required: true, message: t('validation.roleRequired') }]}
              >
                <Select placeholder={t('validation.selectRole')}>
                  {isTenantAdmin && (
                    <Select.Option value="tenant_admin">{t('role.tenant_admin')}</Select.Option>
                  )}
                  {isTenantAdmin && (
                    <Select.Option value="ws_admin">{t('role.ws_admin')}</Select.Option>
                  )}
                  <Select.Option value="member">{t('role.member')}</Select.Option>
                </Select>
              </Form.Item>

              {selectedRole && selectedRole !== 'tenant_admin' && (
                <Form.Item
                  name="workspace_id"
                  label={t('fields.workspace')}
                  rules={[{ required: true, message: t('validation.workspaceRequired') }]}
                >
                  <Select placeholder={t('validation.selectWorkspace')}>
                    {workspaceOptions.map((ws) => (
                      <Select.Option key={ws.id} value={ws.id}>
                        {ws.name}
                      </Select.Option>
                    ))}
                  </Select>
                </Form.Item>
              )}
            </>
          )}

          {drawerMode === 'resetPassword' && (
            <>
              <Form.Item label={t('fields.username')}>
                <Input value={selectedUser?.username || ''} disabled />
              </Form.Item>

              <Form.Item
                name="password"
                label={t('fields.newPassword')}
                rules={[
                  { required: true, message: t('validation.passwordRequired') },
                  { validator: createPasswordValidator((key) => t(key)) },
                ]}
              >
                <Input.Password
                  placeholder={t('validation.inputNewPassword')}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Form.Item>

              <PasswordStrength password={password} />
            </>
          )}
        </Form>
      </Drawer>

      <RoleBindingsDrawer
        user={bindingUser}
        open={Boolean(bindingUser)}
        onClose={() => setBindingUser(null)}
        onChanged={() => actionRef.current?.reload()}
      />
    </>
  );
};

export default UsersTab;
