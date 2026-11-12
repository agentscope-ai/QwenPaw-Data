import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Divider, Form, Input, Spin, Tag, message } from 'antd';
import { LogoutOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { authFetch, changePassword, fetchTenantCurrentUser } from '@/auth';
import type { MeResponse, Workspace } from '@/auth';
import { useAuthStore } from '@/store/authStore';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import PermissionMatrix from '@/pages/UserCenter/components/PermissionMatrix';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage } from '@/services/apiError';
import styles from './index.module.css';

interface ChangePasswordFormValues {
  current_password: string;
  new_password: string;
  confirm_password: string;
}

const ROLE_TAG_COLOR: Record<string, string> = {
  tenant_admin: '#6366f1',
  ws_admin: '#f59e0b',
  member: '#6366f1',
};

const Account: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [form] = Form.useForm<ChangePasswordFormValues>();
  const [me, setMe] = useState<MeResponse | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [changePwdLoading, setChangePwdLoading] = useState(false);
  const [password, setPassword] = useState('');

  const loadProfile = useCallback(async () => {
    try {
      const user = await fetchTenantCurrentUser();
      setMe(user);
      if (user) {
        try {
          const wsResult = await authFetch<{ workspaces: Workspace[] }>(
            '/api/workspaces',
            undefined,
            { session: 'tenant', redirectOnUnauthorized: true },
          );
          setWorkspaces(wsResult.workspaces || []);
        } catch {
          // workspace list may fail for non-admin users
        }
      }
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProfile();
  }, [loadProfile]);

  const handleChangePassword = async (values: ChangePasswordFormValues) => {
    if (!me) return;
    setChangePwdLoading(true);
    try {
      await changePassword({
        current_password: values.current_password,
        new_password: values.new_password,
      });
      await useAuthStore.getState().refresh();
      message.success(t('account.passwordChanged'));
      form.resetFields();
      setPassword('');
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setChangePwdLoading(false);
    }
  };

  const handleLogout = async () => {
    try {
      await useAuthStore.getState().logout();
      message.success(t('account.logoutSuccess'));
    } finally {
      window.location.assign('/login');
    }
  };

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <Card
        className={styles.card}
        title={t('account.profileTitle')}
        extra={
          <Button danger icon={<LogoutOutlined />} onClick={handleLogout}>
            {t('account.logout')}
          </Button>
        }
      >
        <div className={styles.profileGrid}>
          <div className={styles.profileItem}>
            <span className={styles.profileLabel}>{t('fields.username')}</span>
            <span className={styles.profileValueStrong}>
              {me?.display_name || me?.username || '-'}
            </span>
          </div>
          <div className={styles.profileItem}>
            <span className={styles.profileLabel}>{t('fields.role')}</span>
            <div className={styles.profileValue}>
              <Tag color={ROLE_TAG_COLOR[me?.active_workspace_role || ''] || '#94a3b8'}>
                {me?.active_workspace_role ? t(`role.${me.active_workspace_role}`) : '-'}
              </Tag>
            </div>
          </div>
          <div className={styles.profileItem}>
            <span className={styles.profileLabel}>{t('fields.tenant')}</span>
            <span className={styles.profileValue}>{me?.tenant_id || '-'}</span>
          </div>
          <div className={styles.profileItem}>
            <span className={styles.profileLabel}>{t('fields.activeWorkspace')}</span>
            <span className={styles.profileValue}>{me?.active_workspace_name || '-'}</span>
          </div>
        </div>

        {workspaces.length > 0 ? (
          <>
            <Divider />
            <div>
              <h4 className={styles.workspacesTitle}>{t('account.accessibleWorkspaces')}</h4>
              <div className={styles.workspaceRow}>
                {workspaces.map((workspace) => (
                  <article
                    className={`${styles.workspaceCard} ${workspace.id === me?.active_workspace_id ? styles.workspaceCardCurrent : ''}`}
                    key={workspace.id}
                  >
                    <strong title={workspace.name}>{workspace.name}</strong>
                    <Tag color={workspace.status === 'active' ? '#10b981' : '#94a3b8'}>
                      {t(`status.${workspace.status}`)}
                    </Tag>
                    {workspace.id === me?.active_workspace_id ? (
                      <Tag color="#1677ff">{t('account.current')}</Tag>
                    ) : null}
                  </article>
                ))}
              </div>
            </div>
          </>
        ) : null}
      </Card>

      <Card className={styles.card} title={t('permissions.myPermissions')}>
        <PermissionMatrix />
      </Card>

      <Card className={styles.card} title={t('account.changePasswordTitle')}>
        <Form
          form={form}
          layout="horizontal"
          labelAlign="left"
          labelCol={{ flex: '112px' }}
          wrapperCol={{ flex: '1 1 0' }}
          onFinish={handleChangePassword}
          className={styles.passwordForm}
        >
          <Form.Item
            name="current_password"
            label={t('fields.currentPassword')}
            rules={[{ required: true, message: t('validation.currentPasswordRequired') }]}
          >
            <Input.Password placeholder={t('validation.inputCurrentPassword')} />
          </Form.Item>
          <Form.Item
            name="new_password"
            label={t('fields.newPassword')}
            rules={[
              { required: true, message: t('validation.passwordRequired') },
              { validator: createPasswordValidator(t) },
            ]}
          >
            <Input.Password
              placeholder={t('validation.inputNewPassword')}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Form.Item>
          <div className={styles.passwordStrengthRow}>
            <PasswordStrength password={password} />
          </div>
          <Form.Item
            name="confirm_password"
            label={t('fields.confirmPassword')}
            dependencies={['new_password']}
            rules={[
              { required: true, message: t('validation.confirmPasswordRequired') },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve();
                  return Promise.reject(new Error(t('validation.passwordMismatch')));
                },
              }),
            ]}
          >
            <Input.Password placeholder={t('validation.inputConfirmPassword')} />
          </Form.Item>
          <Form.Item label=" " colon={false} className={styles.passwordAction}>
            <Button type="primary" htmlType="submit" loading={changePwdLoading}>
              {t('account.saveNewPassword')}
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
};

export default Account;
