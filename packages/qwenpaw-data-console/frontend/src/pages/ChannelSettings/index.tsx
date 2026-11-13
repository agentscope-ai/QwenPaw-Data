import { ReloadOutlined } from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import { Alert, Form, Space, Spin, Switch, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import {
  channelConfigApi,
  type ChannelConfigResponse,
  type ChannelKey,
  type DingTalkConfig,
  type FeishuConfig,
  type TestResult,
  type WechatConfig,
  type WecomConfig,
} from '@/features/console/api/channelConfig';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader/index';
import QrcodeAuthBlock from '@/features/console/components/QrcodeAuthBlock';
import SecretInput from '@/features/console/components/SecretInput';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Masked secret field: empty in the UI means "don't change", filled means override. */
function secretPlaceholder(
  masked: string | null | undefined,
  t: ReturnType<typeof useAppI18n>['t'],
): string {
  return masked ? t('channel.secretConfigured', { masked }) : t('channel.secretPlaceholder');
}

export default function ChannelSettingsPage() {
  const { t } = useAppI18n();
  const [config, setConfig] = useState<ChannelConfigResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [savingKey, setSavingKey] = useState<ChannelKey | null>(null);
  const [testingKey, setTestingKey] = useState<ChannelKey | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setConfig(await channelConfigApi.get());
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('channel.title')} · QwenPaw Data`;
    void load();
    return () => {
      document.title = previousTitle;
    };
  }, [load, t]);

  const updateChannel = async (channel: ChannelKey, payload: Record<string, unknown>) => {
    setSavingKey(channel);
    try {
      const { config: updated } = await channelConfigApi.update(channel, payload);
      setConfig((prev) => (prev ? { ...prev, [channel]: updated as never } : prev));
      message.success(t('channel.saved'));
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setSavingKey(null);
    }
  };

  const testChannel = async (channel: ChannelKey) => {
    setTestingKey(channel);
    try {
      const result: TestResult = await channelConfigApi.test(channel);
      if (result.success) {
        message.success(result.message);
      } else {
        message.error(result.message);
      }
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setTestingKey(null);
    }
  };

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('channel.title')} />

      <main className={styles.main}>
        <div className={styles.intro}>
          <Button
            aria-label={t('common.refresh')}
            title={t('common.refresh')}
            icon={<ReloadOutlined />}
            onClick={() => void load()}
          />
        </div>

        {loading ? (
          <div className={styles.loading}>
            <Spin />
            <span>{t('channel.loading')}</span>
          </div>
        ) : null}

        {!loading && config ? (
          <div className={styles.sections}>
            {config.feishu ? (
              <FeishuCard
                config={config.feishu}
                saving={savingKey === 'feishu'}
                testing={testingKey === 'feishu'}
                onSave={(p) => updateChannel('feishu', p)}
                onTest={() => testChannel('feishu')}
              />
            ) : null}
            {config.dingtalk ? (
              <DingTalkCard
                config={config.dingtalk}
                saving={savingKey === 'dingtalk'}
                testing={testingKey === 'dingtalk'}
                onSave={(p) => updateChannel('dingtalk', p)}
                onTest={() => testChannel('dingtalk')}
              />
            ) : null}
            {config.wecom ? (
              <WecomCard
                config={config.wecom}
                saving={savingKey === 'wecom'}
                testing={testingKey === 'wecom'}
                onSave={(p) => updateChannel('wecom', p)}
                onTest={() => testChannel('wecom')}
              />
            ) : null}
            {config.wechat ? (
              <WechatCard
                config={config.wechat}
                saving={savingKey === 'wechat'}
                testing={testingKey === 'wechat'}
                onSave={(p) => updateChannel('wechat', p)}
                onTest={() => testChannel('wechat')}
              />
            ) : null}
          </div>
        ) : null}

        {!loading && !config ? <Alert type="info" message={t('channel.empty')} /> : null}
      </main>
    </div>
  );
}

interface CardProps<T> {
  config: T;
  saving: boolean;
  testing: boolean;
  onSave: (payload: Record<string, unknown>) => void;
  onTest: () => void;
}

function FeishuCard(props: CardProps<FeishuConfig>) {
  const [form] = Form.useForm();
  const { t } = useAppI18n();
  return (
    <section className={`${styles.section} qwenpaw-data-card-primary`}>
      <div className={styles.sectionHeading}>
        <span>
          <ChannelIcon channel="feishu" size={22} />
        </span>
        <div>
          <h3>飞书 Feishu</h3>
          <p>{t('channel.feishuDescription')}</p>
        </div>
      </div>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          ...props.config,
          app_secret: '',
          encrypt_key: '',
          verification_token: '',
        }}
        onFinish={(v) => props.onSave(v)}
      >
        <Form.Item label={t('channel.enable')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label={t('channel.streaming')} name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="App ID" name="app_id">
          <Input placeholder="cli_xxx" />
        </Form.Item>
        <Form.Item label="App Secret" name="app_secret">
          <SecretInput placeholder={secretPlaceholder(props.config.app_secret, t)} />
        </Form.Item>
        <Form.Item label="Encrypt Key" name="encrypt_key">
          <SecretInput placeholder={secretPlaceholder(props.config.encrypt_key, t)} />
        </Form.Item>
        <Form.Item label="Verification Token" name="verification_token">
          <SecretInput placeholder={secretPlaceholder(props.config.verification_token, t)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              {t('channel.save')}
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              {t('channel.test')}
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function DingTalkCard(props: CardProps<DingTalkConfig>) {
  const [form] = Form.useForm();
  const { t } = useAppI18n();
  return (
    <section className={`${styles.section} qwenpaw-data-card-primary`}>
      <div className={styles.sectionHeading}>
        <span>
          <ChannelIcon channel="dingtalk" size={22} />
        </span>
        <div>
          <h3>钉钉 DingTalk</h3>
          <p>{t('channel.dingtalkDescription')}</p>
        </div>
      </div>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          ...props.config,
          client_secret: '',
        }}
        onFinish={(v) => props.onSave(v)}
      >
        <Form.Item label={t('channel.enable')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label={t('channel.streaming')} name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="Client ID" name="client_id">
          <Input placeholder="dingxxx" />
        </Form.Item>
        <Form.Item label="Client Secret" name="client_secret">
          <SecretInput placeholder={secretPlaceholder(props.config.client_secret, t)} />
        </Form.Item>
        <Form.Item label="Card Template ID" name="card_template_id">
          <Input placeholder={t('channel.cardTemplatePlaceholder')} />
        </Form.Item>
        <Form.Item label="Card Template Key" name="card_template_key">
          <Input placeholder="content" />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              {t('channel.save')}
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              {t('channel.test')}
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function WecomCard(props: CardProps<WecomConfig>) {
  const [form] = Form.useForm();
  const { t } = useAppI18n();
  return (
    <section className={`${styles.section} qwenpaw-data-card-primary`}>
      <div className={styles.sectionHeading}>
        <span>
          <ChannelIcon channel="wecom" size={22} />
        </span>
        <div>
          <h3>企业微信 WeCom</h3>
          <p>{t('channel.wecomDescription')}</p>
        </div>
      </div>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          ...props.config,
          secret: '',
        }}
        onFinish={(v) => props.onSave(v)}
      >
        <Form.Item label={t('channel.enable')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label={t('channel.streaming')} name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="Bot ID" name="bot_id">
          <Input placeholder={t('channel.wecomBotPlaceholder')} />
        </Form.Item>
        <Form.Item label="Secret" name="secret">
          <SecretInput placeholder={secretPlaceholder(props.config.secret, t)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              {t('channel.save')}
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              {t('channel.test')}
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function WechatCard(props: CardProps<WechatConfig>) {
  const [form] = Form.useForm();
  const { t } = useAppI18n();
  return (
    <section className={`${styles.section} qwenpaw-data-card-primary`}>
      <div className={styles.sectionHeading}>
        <span>
          <ChannelIcon channel="wechat" size={22} />
        </span>
        <div>
          <h3>微信 WeChat</h3>
          <p>{t('channel.wechatDescription')}</p>
        </div>
      </div>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          ...props.config,
          bot_token: '',
        }}
        onFinish={(v) => props.onSave(v)}
      >
        <Form.Item label={t('channel.enable')} name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label={t('channel.streaming')} name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <QrcodeAuthBlock
          label={t('channel.scanLogin')}
          buttonText={t('channel.getWechatQr')}
          imageAlt="WeChat QR Code"
          hintText={t('channel.wechatQrHint')}
          channel="wechat"
          successStatus="confirmed"
          successCredentialKey="bot_token"
          pollInterval={2000}
          onSuccess={(credentials) => {
            form.setFieldsValue({ bot_token: credentials.bot_token });
            message.success(t('channel.wechatLoginSuccess'));
          }}
          onError={(type) => {
            if (type === 'expired') {
              message.warning(t('channel.wechatQrExpired'));
            } else {
              message.error(t('channel.wechatQrFailed'));
            }
          }}
        />
        <Form.Item label="Bot Token" name="bot_token">
          <SecretInput placeholder={secretPlaceholder(props.config.bot_token, t)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              {t('channel.save')}
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              {t('channel.test')}
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}
