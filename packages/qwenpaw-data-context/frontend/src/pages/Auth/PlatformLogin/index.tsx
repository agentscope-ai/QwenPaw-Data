import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Spin, message } from 'antd';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { fetchAuthStatus, platformBootstrap, platformLogin } from '@/auth';
import PasswordStrength from '@/pages/UserCenter/components/PasswordStrength';
import { createPasswordValidator } from '@/pages/UserCenter/components/passwordValidation';
import { getErrorMessage } from '@/services/apiError';

interface PlatformLoginValues {
  bootstrapToken?: string;
  username: string;
  password: string;
}

export default function PlatformLoginPage() {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const [bootstrapRequired, setBootstrapRequired] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);
  const [password, setPassword] = useState('');

  const loadStatus = useCallback(async () => {
    try {
      const status = await fetchAuthStatus();
      if (!status.enabled) {
        message.error(t('login.authNotEnabled'));
        setBootstrapRequired(false);
        return;
      }
      setBootstrapRequired(status.platform_bootstrap_required);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('platformLogin.statusFailed'));
      setBootstrapRequired(false);
    }
  }, [t]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const handleSubmit = async (values: PlatformLoginValues) => {
    setLoading(true);
    try {
      if (bootstrapRequired) {
        await platformBootstrap(values.bootstrapToken || '', values.username, values.password);
        message.success(t('platformLogin.bootstrapSuccess'));
      } else {
        await platformLogin(values.username, values.password);
        message.success(t('platformLogin.loginSuccess'));
      }
      window.location.assign('/user-center/tenants');
    } catch (error: unknown) {
      message.error(
        getErrorMessage(
          error,
          bootstrapRequired ? t('platformLogin.bootstrapFailed') : t('platformLogin.loginFailed'),
        ),
      );
    } finally {
      setLoading(false);
    }
  };

  if (bootstrapRequired === null) {
    return (
      <div style={{ textAlign: 'center', padding: 32 }}>
        <Spin />
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ textAlign: 'center', marginBottom: 24, fontSize: 20 }}>
        {bootstrapRequired ? t('platformLogin.bootstrapTitle') : t('platformLogin.title')}
      </h2>
      {bootstrapRequired && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 20 }}
          message={t('platformLogin.firstLaunch')}
          description={t('platformLogin.bootstrapTokenHint')}
        />
      )}
      <Form<PlatformLoginValues> layout="vertical" onFinish={handleSubmit} autoComplete="off">
        {bootstrapRequired && (
          <Form.Item
            name="bootstrapToken"
            label={t('platformLogin.bootstrapToken')}
            rules={[{ required: true, message: t('platformLogin.bootstrapTokenRequired') }]}
          >
            <Input.Password autoComplete="off" />
          </Form.Item>
        )}
        <Form.Item
          name="username"
          label={t('fields.username')}
          rules={[{ required: true, message: t('validation.usernameRequired') }]}
        >
          <Input autoComplete="username" />
        </Form.Item>
        <Form.Item
          name="password"
          label={t('fields.password')}
          rules={[
            { required: true, message: t('validation.passwordRequired') },
            ...(bootstrapRequired ? [{ validator: createPasswordValidator((key) => t(key)) }] : []),
          ]}
        >
          <Input.Password
            autoComplete={bootstrapRequired ? 'new-password' : 'current-password'}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Form.Item>
        {bootstrapRequired && <PasswordStrength password={password} />}
        <Button
          type="primary"
          htmlType="submit"
          loading={loading}
          block
          size="large"
          style={{ marginTop: 16 }}
        >
          {bootstrapRequired ? t('platformLogin.createAndLogin') : t('platformLogin.submit')}
        </Button>
      </Form>
      <div style={{ textAlign: 'center', marginTop: 16 }}>
        <Link to="/login">{t('platformLogin.backToTenantLogin')}</Link>
      </div>
    </div>
  );
}
