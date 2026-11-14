import { Alert, Button, Card, Form, Input, InputNumber, Space, Tag, message } from '@/design';
import { Descriptions, Typography } from 'antd';
import { CopyOutlined, StopOutlined } from '@ant-design/icons';
import { issueRawCypherGrant, revokeRawCypherGrant, type RawCypherGrantResponse } from '@/auth';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';
import { preserveRawCypherText } from '@/services/rawCypher';

const { Text, Paragraph } = Typography;

interface GrantFormValues {
  actor_user_id: string;
  tenant_id: string;
  workspace_id: string;
  cypher: string;
  params_json: string;
  ttl_seconds: number;
}

interface VisibleGrant extends RawCypherGrantResponse {
  revoked: boolean;
}

function parseParamsObject(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value || '{}');
  if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('params must be a JSON object');
  }
  return parsed as Record<string, unknown>;
}

export default function RawCypherGrantsPage() {
  const { t } = useTranslation();
  const [form] = Form.useForm<GrantFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [grant, setGrant] = useState<VisibleGrant | null>(null);
  const [nowMs, setNowMs] = useState(Date.now());
  const grantExpiresAt = grant?.expires_at;
  const grantRevoked = grant?.revoked;

  useEffect(() => {
    if (!grantExpiresAt || grantRevoked) return undefined;
    const tick = () => {
      const nextNow = Date.now();
      setNowMs(nextNow);
      if (Date.parse(grantExpiresAt) <= nextNow) {
        setGrant((current) => (current ? { ...current, grant_token: '' } : null));
      }
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [grantExpiresAt, grantRevoked]);

  const remainingSeconds = grant
    ? Math.max(0, Math.ceil((Date.parse(grant.expires_at) - nowMs) / 1000))
    : 0;
  const expired = Boolean(grant && !grant.revoked && remainingSeconds === 0);

  const handleIssue = async () => {
    try {
      const values = await form.validateFields();
      const params = parseParamsObject(values.params_json);
      setSubmitting(true);
      const created = await issueRawCypherGrant({
        actor_user_id: values.actor_user_id.trim(),
        tenant_id: values.tenant_id.trim(),
        workspace_id: values.workspace_id.trim(),
        cypher: preserveRawCypherText(values.cypher),
        params,
        ttl_seconds: values.ttl_seconds,
      });
      setGrant({ ...created, revoked: false });
      message.success(t('rawCypher.grant.issueSuccess'));
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('rawCypher.grant.issueFailed')));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCopy = async () => {
    if (!grant?.grant_token || grant.revoked) return;
    try {
      await navigator.clipboard.writeText(grant.grant_token);
      message.success(t('common.copied'));
    } catch {
      message.error(t('common.copyFailed'));
    }
  };

  const handleRevoke = async () => {
    if (!grant || grant.revoked) return;
    setRevoking(true);
    try {
      await revokeRawCypherGrant(grant.grant_id);
      setGrant((current) => (current ? { ...current, grant_token: '', revoked: true } : null));
      message.success(t('rawCypher.grant.revokeSuccess'));
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('rawCypher.grant.revokeFailed')));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <>
      <Alert
        type="warning"
        showIcon
        message={t('rawCypher.grant.warningTitle')}
        description={t('rawCypher.grant.warning')}
        style={{ marginBottom: 16 }}
      />
      <Card title={t('rawCypher.grant.title')}>
        <Form form={form} layout="vertical" initialValues={{ params_json: '{}', ttl_seconds: 300 }}>
          <Space size={16} align="start" wrap>
            <Form.Item
              name="actor_user_id"
              label={t('rawCypher.fields.actorUserId')}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input style={{ width: 260 }} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="tenant_id"
              label={t('rawCypher.fields.tenantId')}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input style={{ width: 260 }} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="workspace_id"
              label={t('rawCypher.fields.workspaceId')}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input style={{ width: 260 }} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="ttl_seconds"
              label={t('rawCypher.fields.ttl')}
              rules={[{ required: true, type: 'number', min: 30, max: 900 }]}
            >
              <InputNumber min={30} max={900} precision={0} style={{ width: 180 }} />
            </Form.Item>
          </Space>
          <Form.Item
            name="cypher"
            label={t('rawCypher.fields.cypher')}
            rules={[
              { required: true, whitespace: true, message: t('rawCypher.validation.cypher') },
            ]}
          >
            <Input.TextArea
              rows={8}
              spellCheck={false}
              style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
            />
          </Form.Item>
          <Form.Item
            name="params_json"
            label={t('rawCypher.fields.params')}
            rules={[
              {
                validator: async (_, value: string) => {
                  try {
                    parseParamsObject(value);
                  } catch {
                    throw new Error(t('rawCypher.validation.params'));
                  }
                },
              },
            ]}
          >
            <Input.TextArea
              rows={5}
              spellCheck={false}
              style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
            />
          </Form.Item>
          <Button type="primary" danger loading={submitting} onClick={handleIssue}>
            {t('rawCypher.grant.issue')}
          </Button>
        </Form>
      </Card>

      {grant && (
        <Card title={t('rawCypher.grant.deliveryTitle')} style={{ marginTop: 24 }}>
          <Alert
            type={grant.revoked || expired ? 'info' : 'error'}
            showIcon
            message={
              grant.revoked
                ? t('rawCypher.grant.revoked')
                : expired
                  ? t('rawCypher.grant.expired')
                  : t('rawCypher.grant.oneTimeWarning')
            }
            style={{ marginBottom: 16 }}
          />
          <Descriptions bordered column={1} size="small">
            <Descriptions.Item label={t('rawCypher.fields.grantId')}>
              <Text copyable>{grant.grant_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('rawCypher.fields.expiresAt')}>
              {grant.expires_at}
            </Descriptions.Item>
            <Descriptions.Item label={t('rawCypher.fields.remaining')}>
              {remainingSeconds}s
            </Descriptions.Item>
            <Descriptions.Item label={t('rawCypher.fields.status')}>
              <Tag color={grant.revoked || expired ? 'default' : 'success'}>
                {grant.revoked
                  ? t('rawCypher.grant.revoked')
                  : expired
                    ? t('rawCypher.grant.expired')
                    : t('rawCypher.grant.active')}
              </Tag>
            </Descriptions.Item>
            {!grant.revoked && !expired && (
              <Descriptions.Item label={t('rawCypher.fields.token')}>
                <Paragraph
                  copyable={{ text: grant.grant_token }}
                  style={{
                    margin: 0,
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                    wordBreak: 'break-all',
                  }}
                >
                  {grant.grant_token}
                </Paragraph>
              </Descriptions.Item>
            )}
          </Descriptions>
          {!grant.revoked && !expired && (
            <Space style={{ marginTop: 16 }}>
              <Button icon={<CopyOutlined />} onClick={handleCopy}>
                {t('rawCypher.grant.copyToken')}
              </Button>
              <Button danger icon={<StopOutlined />} loading={revoking} onClick={handleRevoke}>
                {t('rawCypher.grant.revoke')}
              </Button>
            </Space>
          )}
        </Card>
      )}
    </>
  );
}
