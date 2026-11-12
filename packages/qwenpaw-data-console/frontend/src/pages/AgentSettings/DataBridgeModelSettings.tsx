import { useEffect } from 'react';
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Select,
  Spin,
  Tooltip,
} from 'antd';
import type { FormInstance } from 'antd';
import type { ModelProvider } from '@/features/console/api/types';
import { useAppI18n } from '@/i18n';
import type {
  ActiveModelFormValues,
  useDataBridgeModelSettings,
} from './useDataBridgeModelSettings';
import { useEmbeddingRebuild } from './useEmbeddingRebuild';
import styles from './index.module.less';

interface Props {
  form: FormInstance<ActiveModelFormValues>;
  providers: ModelProvider[];
  settings: ReturnType<typeof useDataBridgeModelSettings>;
  saving: boolean;
}

export default function DataBridgeModelSettings({ form, providers, settings, saving }: Props) {
  const { t } = useAppI18n();
  const rebuild = useEmbeddingRebuild(settings.auth.revision, settings.auth.ready);
  const refreshConfig = settings.load;
  useEffect(() => {
    if (rebuild.job?.status === 'success') void refreshConfig(false);
  }, [rebuild.job?.job_id, rebuild.job?.status, refreshConfig]);
  const providerId = Form.useWatch('cm_provider_id', form);
  const configured = providers.filter((provider) => provider.configured);
  const providerOptions = configured.map((provider) => ({
    value: provider.id,
    label: provider.name,
  }));
  const models = configured.find((provider) => provider.id === providerId)?.models ?? [];
  const required = [{ required: true, message: t('agent.cm.required') }];
  const progress = rebuild.job?.progress;
  const space = settings.config?.embedding_space;
  return (
    <div className={styles.dataBridgeSettings}>
      <div className={styles.sectionHeading}>
        <h3>{t('agent.cm.title')}</h3>
        <p>{t('agent.cm.shared')}</p>
      </div>
      {settings.loading ? <Spin /> : null}
      {settings.error ? (
        <Alert
          type="error"
          showIcon
          message={settings.error}
          action={<Button onClick={() => void settings.load(false)}>{t('common.retry')}</Button>}
        />
      ) : null}
      {settings.config?.configured &&
      (!settings.config.llm.provider_id || !settings.config.embedding.provider_id) ? (
        <Alert type="info" showIcon message={t('agent.cm.legacy')} />
      ) : null}
      <div className={styles.semanticModelGrid}>
        <div className={styles.semanticModelCard}>
          <h4>DataBridge</h4>
          <Form.Item label={t('agent.cm.provider')} name="cm_provider_id" rules={required}>
            <Select options={providerOptions} />
          </Form.Item>
          <Form.Item label={t('agent.cm.model')} name="cm_model_id" rules={required}>
            <Select
              showSearch
              disabled={!providerId}
              options={models.map((model) => ({ value: model.id, label: model.name }))}
            />
          </Form.Item>
          {settings.results.llm ? (
            <Alert
              showIcon
              type={settings.results.llm.success ? 'success' : 'error'}
              message={settings.results.llm.message}
            />
          ) : null}
          <div className={styles.modelTestAction}>
            <Button
              type="primary"
              disabled={saving || settings.loading || !!settings.error || !!settings.testing}
              loading={settings.testing === 'llm'}
              onClick={() => void settings.test('llm')}
            >
              {t('agent.cm.testLlm')}
            </Button>
          </div>
        </div>
        <div className={styles.semanticModelCard}>
          <h4>Embedding</h4>
          <Form.Item
            label={t('agent.cm.embeddingProvider')}
            name="embedding_provider_id"
            rules={required}
          >
            <Select options={providerOptions} />
          </Form.Item>
          <Form.Item
            label={t('agent.cm.embeddingModel')}
            name="embedding_model_id"
            rules={[{ required: true, whitespace: true, message: t('agent.cm.required') }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            label={t('agent.cm.dim')}
            name="embedding_dim"
            rules={[...required, { type: 'integer', min: 1, message: t('agent.cm.dimInvalid') }]}
          >
            <InputNumber min={1} precision={0} />
          </Form.Item>
          {settings.results.embedding ? (
            <Alert
              showIcon
              type={settings.results.embedding.success ? 'success' : 'error'}
              message={settings.results.embedding.message}
            />
          ) : null}
          <div className={styles.modelTestAction}>
            <Button
              type="primary"
              disabled={saving || settings.loading || !!settings.error || !!settings.testing}
              loading={settings.testing === 'embedding'}
              onClick={() => void settings.test('embedding')}
            >
              {t('agent.cm.testEmbedding')}
            </Button>
          </div>
        </div>
      </div>
      {space?.adopted && space.aligned === false ? (
        <Alert
          showIcon
          type="warning"
          message={t('agent.cm.spaceMismatch')}
          description={t('agent.cm.spaceDetails', {
            model: space.model ?? '',
            dim: space.dim ?? '',
            url: space.base_url ?? '',
          })}
        />
      ) : null}
      <div className={styles.rebuildSettings}>
        <div className={styles.rebuildActions}>
          <Popconfirm
            title={t('agent.cm.rebuildConfirm')}
            description={t('agent.cm.rebuildHint')}
            onConfirm={() => rebuild.run()}
            disabled={saving || rebuild.active || rebuild.busy || !settings.config?.configured}
          >
            <Tooltip title={t('agent.cm.rebuildHint')}>
              <Button
                disabled={saving || rebuild.active || !settings.config?.configured}
                loading={rebuild.busy}
              >
                {t('agent.cm.rebuild')}
              </Button>
            </Tooltip>
          </Popconfirm>
        </div>
        {rebuild.job ? (
          <Alert
            showIcon
            type={
              rebuild.job.status === 'failed'
                ? 'error'
                : rebuild.job.status === 'success'
                  ? 'success'
                  : 'info'
            }
            message={t(
              rebuild.job.status === 'pending'
                ? 'agent.cm.rebuildPending'
                : rebuild.job.status === 'running'
                  ? 'agent.cm.rebuildRunning'
                  : rebuild.job.status === 'success'
                    ? 'agent.cm.rebuildSuccess'
                    : 'agent.cm.rebuildFailed',
            )}
            description={
              <div>
                {progress && rebuild.active ? (
                  <>
                    <div>
                      {progress.phase} {progress.current_label}
                    </div>
                    <Progress
                      percent={
                        progress.labels_total
                          ? Math.round((progress.labels_done / progress.labels_total) * 100)
                          : 0
                      }
                    />
                  </>
                ) : null}
                {rebuild.job.error}
              </div>
            }
            action={
              rebuild.job.status === 'failed' ? (
                <Button
                  disabled={saving}
                  loading={rebuild.busy}
                  onClick={() => void rebuild.run(true)}
                >
                  {t('common.retry')}
                </Button>
              ) : undefined
            }
          />
        ) : null}
        {rebuild.error ? <Alert type="error" showIcon message={rebuild.error} /> : null}
      </div>
    </div>
  );
}
