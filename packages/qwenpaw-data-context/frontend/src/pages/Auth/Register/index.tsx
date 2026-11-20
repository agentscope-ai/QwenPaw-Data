import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Form, Input, message } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { AuthApiError, fetchAuthStatus, register } from '@/auth';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage } from '@/services/apiError';
import { redirectAfterAuth } from '@/utils/redirectAfterAuth';

const Register: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [statusChecked, setStatusChecked] = useState(false);
  const [password, setPassword] = useState('');
  const redirectTo = searchParams.get('redirect');

  const checkAuthStatus = useCallback(async () => {
    try {
      const status = await fetchAuthStatus();
      if (!status.enabled) {
        navigate('/login', { replace: true });
        return;
      }
      if (status.platform_bootstrap_required && !status.has_users) {
        navigate('/platform/login', { replace: true });
        return;
      }
      if (!status.legacy_registration_enabled) {
        navigate(redirectTo ? `/login?redirect=${encodeURIComponent(redirectTo)}` : '/login', {
          replace: true,
        });
        return;
      }
      if (status.has_users) {
        navigate(redirectTo ? `/login?redirect=${encodeURIComponent(redirectTo)}` : '/login', {
          replace: true,
        });
        return;
      }
      setStatusChecked(true);
    } catch {
      setStatusChecked(true);
    }
  }, [navigate, redirectTo]);

  useEffect(() => {
    checkAuthStatus();
  }, [checkAuthStatus]);

  const handleSubmit = async (values: {
    username: string;
    password: string;
    confirmPassword: string;
  }) => {
    setLoading(true);
    try {
      const result = await register(values.username, values.password);
      message.success(t('register.success'));
      redirectAfterAuth(redirectTo, result.role);
    } catch (err: unknown) {
      if (err instanceof AuthApiError && err.status === 403) {
        message.error(t('register.closed'));
      } else if (err instanceof AuthApiError && err.status === 409) {
        message.error(t('register.usernameExists'));
      } else if (err instanceof AuthApiError && err.status === 400) {
        message.error(err.message || t('register.invalidInput'));
      } else {
        message.error(getErrorMessage(err, t('register.error')));
      }
    } finally {
      setLoading(false);
    }
  };

  if (!statusChecked) {
    return null;
  }

  return (
    <div>
      <h2 style={{ textAlign: 'center', marginBottom: 24, fontSize: 20, fontWeight: 600 }}>
        {t('register.title')}
      </h2>

      <Alert
        message={t('register.firstUserAdmin')}
        type="info"
        showIcon
        style={{ marginBottom: 24 }}
      />

      <Form form={form} layout="vertical" onFinish={handleSubmit} autoComplete="off">
        <Form.Item
          name="username"
          label={t('fields.username')}
          rules={[
            { required: true, message: t('validation.usernameRequired') },
            { min: 2, max: 32, message: t('validation.usernameLength') },
            { pattern: /^[a-zA-Z0-9_-]+$/, message: t('validation.usernamePattern') },
          ]}
        >
          <Input
            placeholder={t('validation.inputUsername')}
            autoComplete="username"
            allowClear
            size="large"
          />
        </Form.Item>

        <Form.Item
          name="password"
          label={t('fields.password')}
          rules={[
            { required: true, message: t('validation.passwordRequired') },
            { validator: createPasswordValidator(t) },
          ]}
        >
          <Input.Password
            placeholder={t('validation.inputPassword')}
            autoComplete="new-password"
            size="large"
            onChange={(e) => setPassword(e.target.value)}
          />
        </Form.Item>

        <PasswordStrength password={password} />

        <Form.Item
          name="confirmPassword"
          label={t('fields.confirmPassword')}
          dependencies={['password']}
          rules={[
            { required: true, message: t('validation.confirmPasswordRequired') },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue('password') === value) {
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
            size="large"
          />
        </Form.Item>

        <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
          <Button type="primary" htmlType="submit" loading={loading} block size="large">
            {t('register.submit')}
          </Button>
        </Form.Item>
      </Form>
    </div>
  );
};

export default Register;
