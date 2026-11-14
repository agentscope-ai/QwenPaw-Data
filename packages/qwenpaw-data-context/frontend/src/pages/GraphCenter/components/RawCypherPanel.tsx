import { Alert, Button, Form, Input, InputNumber, Modal, Space, message } from '@/design';
import { SafetyCertificateOutlined } from '@ant-design/icons';
import { Typography } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/store/authStore';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';
import { preserveRawCypherText, rawCypherApi, type RawCypherResponse } from '@/services/rawCypher';
import ResultViews from '@/pages/RawCypher/ResultViews';

const { Text, Paragraph } = Typography;

interface RawCypherFormValues {
  cypher: string;
  params_json: string;
  limit: number;
  breakglass_token: string;
}

function parseParamsObject(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value || '{}');
  if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('params must be a JSON object');
  }
  return parsed as Record<string, unknown>;
}

interface RawCypherPanelProps {
  initialCypher?: string;
  onSuccess?: (result: RawCypherResponse, cypher: string) => void;
}

export default function RawCypherPanel({ initialCypher, onSuccess }: RawCypherPanelProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RawCypherFormValues>();
  const activeWorkspaceId = useAuthStore((state) => state.activeWorkspaceId);
  const previousWorkspaceRef = useRef(activeWorkspaceId);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<RawCypherResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const clearToken = useCallback(() => {
    form.setFieldValue('breakglass_token', '');
  }, [form]);

  useEffect(() => {
    if (initialCypher) {
      form.setFieldValue('cypher', initialCypher);
    }
  }, [form, initialCypher]);

  useEffect(() => {
    if (previousWorkspaceRef.current && previousWorkspaceRef.current !== activeWorkspaceId) {
      clearToken();
      setResult(null);
      setError(null);
    }
    previousWorkspaceRef.current = activeWorkspaceId;
  }, [activeWorkspaceId, clearToken]);

  useEffect(
    () => () => {
      form.setFieldValue('breakglass_token', '');
    },
    [form],
  );

  const execute = async (values: RawCypherFormValues) => {
    setExecuting(true);
    setError(null);
    try {
      const cypher = preserveRawCypherText(values.cypher);
      const response = await rawCypherApi.execute(
        {
          cypher,
          params: parseParamsObject(values.params_json),
          limit: values.limit,
        },
        values.breakglass_token.trim(),
      );
      setResult(response);
      message.success(t('rawCypher.executeSuccess'));
      onSuccess?.(response, cypher);
    } catch (err: unknown) {
      setResult(null);
      setError(getErrorMessage(err, t('rawCypher.executeFailed')));
    } finally {
      clearToken();
      setExecuting(false);
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      parseParamsObject(values.params_json);
      Modal.confirm({
        title: t('rawCypher.confirm.title'),
        icon: <SafetyCertificateOutlined style={{ color: '#ff4d4f' }} />,
        width: 640,
        okText: t('rawCypher.confirm.execute'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        content: (
          <Space direction="vertical" size={8}>
            <Paragraph>{t('rawCypher.confirm.writeRisk')}</Paragraph>
            <Paragraph>{t('rawCypher.confirm.bindingRisk')}</Paragraph>
            <Paragraph>{t('rawCypher.confirm.consumeRisk')}</Paragraph>
            <Paragraph>{t('rawCypher.confirm.countRisk')}</Paragraph>
          </Space>
        ),
        onOk: () => execute(values),
      });
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      message.error(getErrorMessage(err, t('rawCypher.paramsInvalid')));
    }
  };

  return (
    <div>
      <div style={{ marginBottom: 10, fontSize: 15, fontWeight: 600 }}>
        {t('rawCypher.executorTitle')}
      </div>
      <Alert
        type="warning"
        showIcon
        message={t('rawCypher.executorWarningTitle')}
        description={t('rawCypher.executorWarning')}
        style={{ marginBottom: 12 }}
      />
      <Form
        form={form}
        layout="vertical"
        autoComplete="off"
        initialValues={{
          cypher: initialCypher || '',
          params_json: '{}',
          limit: 200,
          breakglass_token: '',
        }}
      >
        <Form.Item
          name="cypher"
          label={t('rawCypher.fields.cypher')}
          rules={[{ required: true, whitespace: true, message: t('rawCypher.validation.cypher') }]}
        >
          <Input.TextArea
            rows={6}
            spellCheck={false}
            placeholder={t('rawCypher.placeholders.cypher')}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
          />
        </Form.Item>
        <Form.Item
          name="params_json"
          label={t('rawCypher.fields.params')}
          rules={[
            { required: true, message: t('rawCypher.validation.params') },
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
          extra={t('rawCypher.paramsScopeHint')}
        >
          <Input.TextArea
            rows={4}
            spellCheck={false}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
          />
        </Form.Item>
        <div style={{ display: 'grid', gridTemplateColumns: '140px minmax(0, 1fr)', gap: 12 }}>
          <Form.Item
            name="limit"
            label={t('rawCypher.fields.limit')}
            rules={[{ required: true, type: 'number', min: 1, max: 1000 }]}
          >
            <InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="breakglass_token"
            label={t('rawCypher.fields.token')}
            rules={[{ required: true, whitespace: true, message: t('rawCypher.validation.token') }]}
            extra={t('rawCypher.tokenMemoryHint')}
          >
            <Input.Password
              autoComplete="off"
              visibilityToggle
              placeholder={t('rawCypher.placeholders.token')}
            />
          </Form.Item>
        </div>
        {error && (
          <Alert
            type="error"
            showIcon
            message={t('rawCypher.executeFailed')}
            description={error}
            style={{ marginBottom: 12 }}
          />
        )}
        <Space>
          <Button type="primary" danger loading={executing} onClick={handleSubmit}>
            {t('rawCypher.execute')}
          </Button>
          <Button
            onClick={() => {
              form.resetFields();
              if (initialCypher) form.setFieldValue('cypher', initialCypher);
              setResult(null);
              setError(null);
            }}
          >
            {t('common.reset')}
          </Button>
          <Text type="secondary">{t('rawCypher.workspace', { id: activeWorkspaceId })}</Text>
        </Space>
      </Form>
      {result && (
        <div style={{ marginTop: 16 }}>
          <ResultViews result={result} />
        </div>
      )}
    </div>
  );
}
