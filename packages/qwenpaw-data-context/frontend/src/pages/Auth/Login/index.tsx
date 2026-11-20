import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Form, Input, message } from 'antd';
import { useNavigate, useSearchParams, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { AuthApiError, fetchAuthStatus, login } from '@/auth';
import { getErrorMessage } from '@/services/apiError';
import { getFirstAllowedPage, redirectAfterAuth } from '@/utils/redirectAfterAuth';
import DevAccountTabs from './DevAccountTabs';

const Login: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [statusChecked, setStatusChecked] = useState(false);
  const [authDisabled, setAuthDisabled] = useState(false);
  const [tenantOnboardingPending, setTenantOnboardingPending] = useState(false);
  const [legacyRegistrationEnabled, setLegacyRegistrationEnabled] = useState(false);

  const redirectTo = searchParams.get('redirect');

  const checkAuthStatus = useCallback(async () => {
    try {
      const status = await fetchAuthStatus();
      if (!status.enabled) {
        setAuthDisabled(true);
        setStatusChecked(true);
        return;
      }
      setLegacyRegistrationEnabled(status.legacy_registration_enabled);
      if (status.platform_bootstrap_required && !status.has_users) {
        navigate('/platform/login', { replace: true });
        return;
      }
      if (!status.has_users) {
        setTenantOnboardingPending(true);
        setStatusChecked(true);
        return;
      }
      setStatusChecked(true);
    } catch {
      setStatusChecked(true);
    }
  }, [navigate]);

  useEffect(() => {
    checkAuthStatus();
  }, [checkAuthStatus]);

  const handleSubmit = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const result = await login(values.username, values.password);
      message.success(t('login.success'));
      if (result.must_change_password) {
        const target = `/user-center/account/change-password?redirect=${encodeURIComponent(
          redirectTo || getFirstAllowedPage(result.role),
        )}`;
        window.location.assign(target);
      } else {
        redirectAfterAuth(redirectTo, result.role);
      }
    } catch (err: unknown) {
      if (err instanceof AuthApiError && err.status === 401) {
        message.error(t('login.invalidCredentials'));
      } else if (err instanceof AuthApiError && err.status === 429) {
        const body =
          err.body && typeof err.body === 'object'
            ? (err.body as { retry_after?: unknown })
            : undefined;
        const retryAfter = Number(body?.retry_after);
        if (retryAfter) {
          message.error(t('login.rateLimited', { seconds: retryAfter }));
        } else {
          message.error(t('login.tooManyAttempts'));
        }
      } else {
        message.error(getErrorMessage(err, t('login.error')));
      }
    } finally {
      setLoading(false);
    }
  };

  if (!statusChecked) {
    return null;
  }

  if (authDisabled) {
    return (
      <div style={{ textAlign: 'center', padding: '24px 0' }}>
        <p style={{ color: 'var(--qwenpaw-data-color-text-secondary)' }}>{t('login.authNotEnabled')}</p>
      </div>
    );
  }

  if (tenantOnboardingPending) {
    return (
      <div>
        <Alert
          type="info"
          showIcon
          message={t('login.onboardingPendingTitle')}
          description={t('login.onboardingPendingDescription')}
        />
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <Link to="/platform/login">{t('login.platformAdminLogin')}</Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ textAlign: 'center', marginBottom: 24, fontSize: 20, fontWeight: 600 }}>
        {t('login.title')}
      </h2>

      <Form form={form} layout="vertical" onFinish={handleSubmit} autoComplete="off">
        {/* 联调账号快填条只在 dev server 渲染，生产构建不展示示例账号。 */}
        {import.meta.env.DEV ? <DevAccountTabs form={form} /> : null}

        <Form.Item
          name="username"
          label={t('fields.username')}
          rules={[{ required: true, message: t('validation.usernameRequired') }]}
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
          rules={[{ required: true, message: t('validation.passwordRequired') }]}
        >
          <Input.Password
            placeholder={t('validation.inputPassword')}
            autoComplete="current-password"
            size="large"
          />
        </Form.Item>

        <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
          <Button type="primary" htmlType="submit" loading={loading} block size="large">
            {t('login.submit')}
          </Button>
        </Form.Item>
      </Form>

      {legacyRegistrationEnabled && (
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <Link to="/register">{t('login.registerLink')}</Link>
        </div>
      )}
    </div>
  );
};

export default Login;
