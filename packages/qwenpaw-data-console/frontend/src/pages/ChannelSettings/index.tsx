import { MenuOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import { SparkLeftArrowLine } from '@agentscope-ai/icons';
import { Alert, Form, Space, Spin, Switch, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  channelConfigApi,
  type ChannelConfigResponse,
  type ChannelKey,
  type TestResult,
} from '@/features/console/api/channelConfig';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import ConsoleSettingsMenu from '@/features/console/components/ConsoleSettingsMenu';
import QrcodeAuthBlock from '@/features/console/components/QrcodeAuthBlock';
import SecretInput from '@/features/console/components/SecretInput';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import styles from './index.module.less';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Masked secret field: empty in the UI means "don't change", filled means override. */
function secretPlaceholder(masked: string | null | undefined): string {
  return masked ? `已配置（${masked}），留空不修改` : '请输入';
}

export default function ChannelSettingsPage() {
  const navigate = useNavigate();
  const { openMobileNav } = useConsoleLayout();
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
    document.title = 'Channel Configuration · QwenPaw Data';
    void load();
    return () => {
      document.title = previousTitle;
    };
  }, [load]);

  const updateChannel = async (
    channel: ChannelKey,
    payload: Record<string, unknown>,
  ) => {
    setSavingKey(channel);
    try {
      const { config: updated } = await channelConfigApi.update(channel, payload);
      setConfig((prev) =>
        prev ? { ...prev, [channel]: updated as never } : prev,
      );
      message.success('配置已保存，channel 已重连');
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
      <header className={styles.header}>
        <Button
          className={styles.mobileMenu}
          type="text"
          icon={<MenuOutlined />}
          aria-label="打开导航"
          onClick={openMobileNav}
        />
        <button
          className={styles.backButton}
          type="button"
          aria-label="返回上一页"
          title="返回上一页"
          onClick={() => navigate(-1)}
        >
          <SparkLeftArrowLine size={18} />
        </button>
        <h1>Channel Configuration</h1>
        <ConsoleSettingsMenu className={styles.headerSettings} />
      </header>

      <main className={styles.main}>
        <div className={styles.intro}>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
        </div>

        {loading ? (
          <div className={styles.loading}><Spin /><span>正在加载 channel 配置…</span></div>
        ) : null}

        {!loading && config ? (
          <div className={styles.sections}>
            <FeishuCard
              config={config.feishu}
              saving={savingKey === 'feishu'}
              testing={testingKey === 'feishu'}
              onSave={(p) => updateChannel('feishu', p)}
              onTest={() => testChannel('feishu')}
            />
            <DingTalkCard
              config={config.dingtalk}
              saving={savingKey === 'dingtalk'}
              testing={testingKey === 'dingtalk'}
              onSave={(p) => updateChannel('dingtalk', p)}
              onTest={() => testChannel('dingtalk')}
            />
            <WecomCard
              config={config.wecom}
              saving={savingKey === 'wecom'}
              testing={testingKey === 'wecom'}
              onSave={(p) => updateChannel('wecom', p)}
              onTest={() => testChannel('wecom')}
            />
            <WechatCard
              config={config.wechat}
              saving={savingKey === 'wechat'}
              testing={testingKey === 'wechat'}
              onSave={(p) => updateChannel('wechat', p)}
              onTest={() => testChannel('wechat')}
            />
          </div>
        ) : null}

        {!loading && !config ? (
          <Alert type="info" message="暂无 channel 配置" />
        ) : null}
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

function FeishuCard(props: CardProps<ChannelConfigResponse['feishu']>) {
  const [form] = Form.useForm();
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeading}>
        <span><ChannelIcon channel="feishu" size={22} /></span>
        <div>
          <h3>飞书 Feishu</h3>
          <p>App ID 与 Secret 来自飞书开放平台；保存后 host-core 自动重连。</p>
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
        <Form.Item label="启用" name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="流式回复" name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="App ID" name="app_id">
          <Input placeholder="cli_xxx" />
        </Form.Item>
        <Form.Item label="App Secret" name="app_secret">
          <SecretInput placeholder={secretPlaceholder(props.config.app_secret)} />
        </Form.Item>
        <Form.Item label="Encrypt Key" name="encrypt_key">
          <SecretInput placeholder={secretPlaceholder(props.config.encrypt_key)} />
        </Form.Item>
        <Form.Item label="Verification Token" name="verification_token">
          <SecretInput placeholder={secretPlaceholder(props.config.verification_token)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              保存
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              测试
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function DingTalkCard(props: CardProps<ChannelConfigResponse['dingtalk']>) {
  const [form] = Form.useForm();
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeading}>
        <span><ChannelIcon channel="dingtalk" size={22} /></span>
        <div>
          <h3>钉钉 DingTalk</h3>
          <p>Client ID 与 Secret 来自钉钉开发者后台；保存后 host-core 自动重连。</p>
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
        <Form.Item label="启用" name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="流式回复" name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="Client ID" name="client_id">
          <Input placeholder="dingxxx" />
        </Form.Item>
        <Form.Item label="Client Secret" name="client_secret">
          <SecretInput placeholder={secretPlaceholder(props.config.client_secret)} />
        </Form.Item>
        <Form.Item label="Card Template ID" name="card_template_id">
          <Input placeholder="流式卡片模板 ID（可选）" />
        </Form.Item>
        <Form.Item label="Card Template Key" name="card_template_key">
          <Input placeholder="content" />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              保存
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              测试
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function WecomCard(props: CardProps<ChannelConfigResponse['wecom']>) {
  const [form] = Form.useForm();
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeading}>
        <span><ChannelIcon channel="wecom" size={22} /></span>
        <div>
          <h3>企业微信 WeCom</h3>
          <p>智能机器人 Bot ID 与 Secret；保存后 host-core 自动重连。</p>
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
        <Form.Item label="启用" name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="流式回复" name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="Bot ID" name="bot_id">
          <Input placeholder="企业微信智能机器人 bot_id" />
        </Form.Item>
        <Form.Item label="Secret" name="secret">
          <SecretInput placeholder={secretPlaceholder(props.config.secret)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              保存
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              测试
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}

function WechatCard(props: CardProps<ChannelConfigResponse['wechat']>) {
  const [form] = Form.useForm();
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeading}>
        <span><ChannelIcon channel="wechat" size={22} /></span>
        <div>
          <h3>微信 WeChat</h3>
          <p>iLink Bot Token 来自微信 iLink Bot 平台；保存后 host-core 自动重连。</p>
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
        <Form.Item label="启用" name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="流式回复" name="streaming_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <QrcodeAuthBlock
          label="扫码登录"
          buttonText="获取微信二维码"
          imageAlt="WeChat QR Code"
          hintText="使用微信扫码授权登录，成功后 Bot Token 自动填入下方"
          channel="wechat"
          successStatus="confirmed"
          successCredentialKey="bot_token"
          pollInterval={2000}
          onSuccess={(credentials) => {
            form.setFieldsValue({ bot_token: credentials.bot_token });
            message.success('微信登录成功，Bot Token 已填入');
          }}
          onError={(type) => {
            if (type === 'expired') {
              message.warning('微信二维码已过期，请重新获取');
            } else {
              message.error('微信二维码获取失败，请重试');
            }
          }}
        />
        <Form.Item label="Bot Token" name="bot_token">
          <SecretInput placeholder={secretPlaceholder(props.config.bot_token)} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={props.saving}>
              保存
            </Button>
            <Button loading={props.testing} onClick={props.onTest}>
              测试
            </Button>
          </Space>
        </Form.Item>
      </Form>
    </section>
  );
}
