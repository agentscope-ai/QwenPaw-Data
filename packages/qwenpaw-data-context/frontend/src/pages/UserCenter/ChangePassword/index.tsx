import React, { useState } from 'react';
import { Alert, Button, Card, Form, Input, message } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { changePassword } from '@/auth';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage } from '@/services/apiError';
import { getSafeLocalRedirect, useAuthStore } from '@/store/authStore';

interface ChangePasswordFormValues {
  current_password?: string;
  new_password: string;
  confirm_password: string;
}

const ChangePassword: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [form] = Form.useForm<ChangePasswordFormValues>();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [password, setPassword] = useState('');
  const mustChangePassword = useAuthStore((state) => state.mustChangePassword);

  const redirectTo = getSafeLocalRedirect(searchParams.get('redirect'));

  const handleSubmit = async (values: ChangePasswordFormValues) => {
    setLoading(true);
    try {
      await changePassword({
        current_password: mustChangePassword ? undefined : values.current_password,
        new_password: values.new_password,
      });
      await useAuthStore.getState().refresh();
      message.success(t('changePassword.success'));
      // Host console is a separate SPA; in-app navigate would 404 inside DataBridge.
      window.location.replace(redirectTo);
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('common.operationFailed')));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: '4px 0 24px' }}>
      <Card title={t('changePassword.title')} style={{ maxWidth: 560 }}>
        {mustChangePassword && (
          <Alert
            message={t('changePassword.adminResetNotice')}
            type="warning"
            showIcon
            style={{ marginBottom: 24 }}
          />
        )}

        <Form form={form} layout="vertical" onFinish={handleSubmit} autoComplete="off">
          {!mustChangePassword && (
            <Form.Item
              name="current_password"
              label={t('fields.currentPassword')}
              rules={[
                {
                  required: true,
                  message: t('validation.currentPasswordRequired'),
                },
              ]}
            >
              <Input.Password
                placeholder={t('validation.inputCurrentPassword')}
                autoComplete="current-password"
              />
            </Form.Item>
          )}
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
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Form.Item>

          <PasswordStrength password={password} />

          <Form.Item
            name="confirm_password"
            label={t('fields.confirmPassword')}
            dependencies={['new_password']}
            rules={[
              { required: true, message: t('validation.confirmPasswordRequired') },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error(t('validation.passwordMismatch')));
                },
              }),
            ]}
            style={{ marginTop: 16 }}
          >
            <Input.Password
              placeholder={t('validation.inputConfirmPassword')}
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" loading={loading} block>
              {t('changePassword.submit')}
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
};

export default ChangePassword;
