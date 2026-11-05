import {
  CheckCircleFilled,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  MenuOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { Button, Input, Select } from '@agentscope-ai/design';
import { SparkLeftArrowLine } from '@agentscope-ai/icons';
import {
  Alert,
  Form,
  InputNumber,
  Modal,
  Popconfirm,
  Spin,
  Switch,
  Tag,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { dataConsoleApi } from '@/features/console/api/client';
import ConsoleSettingsMenu from '@/features/console/components/ConsoleSettingsMenu';
import SecretInput from '@/features/console/components/SecretInput';
import type {
  ModelProvider,
  ProviderModel,
  RuntimeSettings,
} from '@/features/console/api/types';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import styles from './index.module.less';

interface ActiveModelFormValues {
  default_provider_id: string;
  default_model_id: string;
  light_selection?: string;
}

interface ProviderCardProps {
  provider: ModelProvider;
  onChange: (provider: ModelProvider) => void;
}

interface ProviderModelsProps {
  provider: ModelProvider;
  onChange: (providerId: string, models: ProviderModel[]) => void;
}

type ThinkingMode = 'inherit' | 'enabled' | 'disabled';

function isExtraModelEditor(editing: ProviderModel | 'extra' | null): boolean {
  return editing === 'extra'
    || (typeof editing === 'object' && editing?.source === 'extra');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function modelSelection(providerId: string | null, modelId: string | null) {
  return providerId && modelId ? `${providerId}/${modelId}` : undefined;
}

function parseModelSelection(value?: string): [string | null, string | null] {
  if (!value) return [null, null];
  const separator = value.indexOf('/');
  if (separator < 1) return [null, null];
  return [value.slice(0, separator), value.slice(separator + 1)];
}

function ProviderCard({ provider, onChange }: ProviderCardProps) {
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  const save = async () => {
    const trimmedKey = apiKey.trim();
    if (!provider.configured && !trimmedKey) {
      message.warning('首次配置 Provider 时必须填写 API Key');
      return;
    }
    setSaving(true);
    try {
      const { provider: updated } = await dataConsoleApi.updateModelProvider(
        provider.id,
        {
          ...(trimmedKey ? { api_key: trimmedKey } : {}),
          base_url: baseUrl.trim() || null,
        },
      );
      setApiKey('');
      setBaseUrl(updated.base_url);
      onChange(updated);
      message.success(`${provider.name} 配置已保存`);
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    setClearing(true);
    try {
      await dataConsoleApi.clearModelProvider(provider.id);
      onChange({
        ...provider,
        configured: false,
        api_key_masked: '',
        base_url: provider.catalog_base_url,
      });
      setApiKey('');
      setBaseUrl(provider.catalog_base_url);
      message.success(`${provider.name} 凭证已清除`);
    } catch (error) {
      message.error(`清除失败：${errorMessage(error)}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <article className={styles.providerCard}>
      <header>
        <div className={styles.providerIdentity}>
          <span className={styles.providerIcon}><KeyOutlined /></span>
          <div>
            <strong>{provider.name}</strong>
            <span>{provider.chat_model}</span>
          </div>
        </div>
        <Tag
          color={provider.configured ? 'success' : 'default'}
          icon={provider.configured ? <CheckCircleFilled /> : undefined}
        >
          {provider.configured ? '已配置' : '未配置'}
        </Tag>
      </header>

      <div className={styles.providerFields}>
        <label>
          <span>API Key</span>
          <SecretInput
            autoComplete="new-password"
            value={apiKey}
            placeholder={provider.configured
              ? `已保存 ${provider.api_key_masked}，留空表示不修改`
              : `请输入 ${provider.api_key_prefix} 开头的 API Key`}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </label>
        <label>
          <span>Base URL</span>
          <Input
            value={baseUrl}
            placeholder={provider.catalog_base_url || '请输入 OpenAI Compatible Base URL'}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>
      </div>

      <footer>
        {provider.configured ? (
          <Button
            danger
            type="text"
            icon={<CloseCircleOutlined />}
            loading={clearing}
            onClick={() => void clear()}
          >
            清除凭证
          </Button>
        ) : <span />}
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          onClick={() => void save()}
        >
          保存 Provider
        </Button>
      </footer>
    </article>
  );
}

function ProviderModels({ provider, onChange }: ProviderModelsProps) {
  const [editing, setEditing] = useState<ProviderModel | 'extra' | null>(null);
  const [modelId, setModelId] = useState('');
  const [name, setName] = useState('');
  const [thinkingMode, setThinkingMode] = useState<ThinkingMode>('inherit');
  const [generateKwargs, setGenerateKwargs] = useState('{}');
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const closeEditor = () => {
    if (saving) return;
    setEditing(null);
  };

  const openEditor = (model: ProviderModel | 'extra') => {
    setEditing(model);
    if (model === 'extra') {
      setModelId('');
      setName('');
      setThinkingMode('inherit');
      setGenerateKwargs('{}');
      return;
    }
    setModelId(model.id);
    setName(model.name);
    setThinkingMode(
      model.thinking_enabled === null
        ? 'inherit'
        : model.thinking_enabled
          ? 'enabled'
          : 'disabled',
    );
    setGenerateKwargs(JSON.stringify(model.generate_kwargs ?? {}, null, 2));
  };

  const saveModel = async () => {
    const trimmedModelId = modelId.trim();
    const source = isExtraModelEditor(editing) ? 'extra' : 'override';
    if (!trimmedModelId) {
      message.warning('请输入模型 ID');
      return;
    }
    if (source === 'extra' && !name.trim()) {
      message.warning('请输入模型名称');
      return;
    }

    let parsedKwargs: Record<string, unknown>;
    try {
      const parsed = JSON.parse(generateKwargs || '{}') as unknown;
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error('not an object');
      }
      parsedKwargs = parsed as Record<string, unknown>;
    } catch {
      message.error('生成参数必须是合法的 JSON 对象');
      return;
    }

    setSaving(true);
    try {
      const { model } = await dataConsoleApi.upsertProviderModel(
        provider.id,
        trimmedModelId,
        {
          source,
          name: source === 'extra' ? name.trim() : null,
          thinking_enabled: thinkingMode === 'inherit'
            ? null
            : thinkingMode === 'enabled',
          generate_kwargs: parsedKwargs,
        },
      );
      const nextModels = provider.models.some((item) => item.id === model.id)
        ? provider.models.map((item) => item.id === model.id ? model : item)
        : [...provider.models, model];
      onChange(provider.id, nextModels);
      setEditing(null);
      message.success(`${model.name} 已保存`);
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSaving(false);
    }
  };

  const deleteModel = async (model: ProviderModel) => {
    setDeletingId(model.id);
    try {
      await dataConsoleApi.deleteProviderModel(provider.id, model.id);
      const { models } = await dataConsoleApi.getProviderModels(provider.id);
      onChange(provider.id, models);
      message.success(model.source === 'override' ? '模型已恢复为内置配置' : '扩展模型已删除');
    } catch (error) {
      message.error(`操作失败：${errorMessage(error)}`);
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <article className={styles.modelProviderCard}>
      <header>
        <div>
          <strong>{provider.name}</strong>
          <span>{provider.models.length} 个可用模型</span>
        </div>
        <Button size="small" icon={<PlusOutlined />} onClick={() => openEditor('extra')}>
          添加模型
        </Button>
      </header>
      <div className={styles.modelList}>
        {provider.models.map((model) => (
          <div className={styles.modelRow} key={model.id}>
            <div className={styles.modelSummary}>
              <div>
                <strong>{model.name}</strong>
                <code>{model.id}</code>
              </div>
              <div className={styles.modelTags}>
                <Tag color={model.source === 'catalog' ? 'default' : 'blue'}>
                  {model.source === 'catalog' ? '内置' : model.source === 'extra' ? '扩展' : '已覆盖'}
                </Tag>
                {model.thinking_enabled !== null ? (
                  <Tag>{model.thinking_enabled ? 'Thinking 开启' : 'Thinking 关闭'}</Tag>
                ) : null}
              </div>
            </div>
            <div className={styles.modelActions}>
              <Button
                size="small"
                type="text"
                icon={<EditOutlined />}
                onClick={() => openEditor(model)}
              >
                {model.source === 'catalog' ? '覆盖配置' : '编辑'}
              </Button>
              {model.source !== 'catalog' ? (
                <Popconfirm
                  title={model.source === 'override' ? '恢复内置模型配置？' : '删除这个扩展模型？'}
                  okText="确认"
                  cancelText="取消"
                  onConfirm={() => void deleteModel(model)}
                >
                  <Button
                    danger
                    size="small"
                    type="text"
                    icon={<DeleteOutlined />}
                    loading={deletingId === model.id}
                  >
                    {model.source === 'override' ? '恢复内置' : '删除'}
                  </Button>
                </Popconfirm>
              ) : null}
            </div>
          </div>
        ))}
      </div>

      <Modal
        open={editing !== null}
        title={editing === 'extra' ? `为 ${provider.name} 添加模型` : '编辑模型配置'}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onCancel={closeEditor}
        onOk={() => void saveModel()}
      >
        <div className={styles.modelEditor}>
          <label>
            <span>模型 ID</span>
            <Input
              value={modelId}
              disabled={editing !== 'extra'}
              placeholder="例如 qwen-plus"
              onChange={(event) => setModelId(event.target.value)}
            />
          </label>
          {isExtraModelEditor(editing) ? (
            <label>
              <span>模型名称</span>
              <Input
                value={name}
                placeholder="用于界面展示"
                onChange={(event) => setName(event.target.value)}
              />
            </label>
          ) : null}
          <label>
            <span>Thinking 模式</span>
            <Select
              value={thinkingMode}
              options={[
                { value: 'inherit', label: '跟随默认配置' },
                { value: 'enabled', label: '开启' },
                { value: 'disabled', label: '关闭' },
              ]}
              onChange={setThinkingMode}
            />
          </label>
          <label>
            <span>生成参数（JSON）</span>
            <Input.TextArea
              rows={6}
              value={generateKwargs}
              spellCheck={false}
              placeholder={'{\n  "temperature": 0.7\n}'}
              onChange={(event) => setGenerateKwargs(event.target.value)}
            />
          </label>
        </div>
      </Modal>
    </article>
  );
}

export default function AgentSettingsPage() {
  const navigate = useNavigate();
  const { openMobileNav } = useConsoleLayout();
  const [activeForm] = Form.useForm<ActiveModelFormValues>();
  const [runtimeForm] = Form.useForm<RuntimeSettings>();
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingModels, setSavingModels] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [providerResponse, activeResponse, runtimeResponse] = await Promise.all([
        dataConsoleApi.getModelProviders(),
        dataConsoleApi.getActiveModels(),
        dataConsoleApi.getRuntimeSettings(),
      ]);
      setProviders(providerResponse.providers);
      const active = activeResponse.active_models;
      activeForm.setFieldsValue({
        default_provider_id: active.default_provider_id ?? undefined,
        default_model_id: active.default_model_id ?? undefined,
        light_selection: modelSelection(
          active.light_provider_id,
          active.light_model_id,
        ),
      });
      runtimeForm.setFieldsValue(runtimeResponse.runtime_settings);
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [activeForm, runtimeForm]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'Agent Configuration · QwenPaw Data';
    void load();
    return () => {
      document.title = previousTitle;
    };
  }, [load]);

  const configuredProviders = useMemo(
    () => providers.filter((provider) => provider.configured),
    [providers],
  );
  const defaultProviderId = Form.useWatch('default_provider_id', activeForm);
  const defaultModelOptions = useMemo(
    () => providers.find((provider) => provider.id === defaultProviderId)?.models
      .map((model) => ({ value: model.id, label: model.name })) ?? [],
    [defaultProviderId, providers],
  );
  const allModelOptions = useMemo(
    () => configuredProviders.flatMap((provider) => provider.models.map((model) => ({
      value: `${provider.id}/${model.id}`,
      label: `${provider.name} · ${model.name}`,
    }))),
    [configuredProviders],
  );

  const updateProvider = useCallback((updated: ModelProvider) => {
    setProviders((current) => current.map((provider) => (
      provider.id === updated.id ? updated : provider
    )));
    if (!updated.configured) {
      if (activeForm.getFieldValue('default_provider_id') === updated.id) {
        activeForm.setFieldsValue({
          default_provider_id: undefined,
          default_model_id: undefined,
        });
      }
      const lightSelection = activeForm.getFieldValue('light_selection');
      if (lightSelection?.startsWith(`${updated.id}/`)) {
        activeForm.setFieldValue('light_selection', undefined);
      }
    }
  }, [activeForm]);

  const updateProviderModels = useCallback((providerId: string, models: ProviderModel[]) => {
    setProviders((current) => current.map((provider) => (
      provider.id === providerId ? { ...provider, models } : provider
    )));
  }, []);

  const saveActiveModels = async (values: ActiveModelFormValues) => {
    const [lightProviderId, lightModelId] = parseModelSelection(values.light_selection);
    setSavingModels(true);
    try {
      const { active_models: saved } = await dataConsoleApi.updateActiveModels({
        default_provider_id: values.default_provider_id,
        default_model_id: values.default_model_id,
        light_provider_id: lightProviderId,
        light_model_id: lightModelId,
      });
      activeForm.setFieldsValue({
        ...values,
        light_selection: modelSelection(saved.light_provider_id, saved.light_model_id),
      });
      message.success('激活模型已更新');
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSavingModels(false);
    }
  };

  const saveRuntimeSettings = async (values: RuntimeSettings) => {
    setSavingRuntime(true);
    try {
      const { runtime_settings: saved } = await dataConsoleApi.updateRuntimeSettings(values);
      runtimeForm.setFieldsValue(saved);
      message.success('运行参数已更新');
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSavingRuntime(false);
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
        <h1>Agent Configuration</h1>
        <ConsoleSettingsMenu className={styles.headerSettings} />
      </header>

      <main className={styles.main}>
        <div className={styles.intro}>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
        </div>

        {loading ? (
          <div className={styles.loading}><Spin /><span>正在加载模型配置…</span></div>
        ) : null}
        {!loading && loadError ? (
          <Alert
            showIcon
            type="error"
            message="模型配置加载失败"
            description={loadError}
            action={<Button size="small" onClick={() => void load()}>重试</Button>}
          />
        ) : null}

        {!loading && !loadError ? (
          <div className={styles.sections}>
            <section className={styles.section}>
              <div className={styles.sectionHeading}>
                <span><KeyOutlined /></span>
                <div>
                  <h3>Provider 凭证</h3>
                  <p>API Key 只在保存时提交，读取接口仅返回脱敏内容。</p>
                </div>
              </div>
              <div className={styles.providerGrid}>
                {providers.map((provider) => (
                  <ProviderCard
                    key={provider.id}
                    provider={provider}
                    onChange={updateProvider}
                  />
                ))}
              </div>
            </section>

            <section className={styles.section}>
              <div className={styles.sectionHeading}>
                <span><SettingOutlined /></span>
                <div>
                  <h3>Provider 模型</h3>
                  <p>扩展新的模型 ID，或覆盖内置模型的 Thinking 与生成参数。</p>
                </div>
              </div>
              <div className={styles.modelProviderGrid}>
                {providers.map((provider) => (
                  <ProviderModels
                    key={provider.id}
                    provider={provider}
                    onChange={updateProviderModels}
                  />
                ))}
              </div>
            </section>

            <section className={styles.section}>
              <div className={styles.sectionHeading}>
                <span><SettingOutlined /></span>
                <div>
                  <h3>激活模型</h3>
                  <p>默认模型用于主 Agent，轻量模型用于业务轨迹和 Segment 摘要。</p>
                </div>
              </div>
              {!configuredProviders.length ? (
                <Alert
                  showIcon
                  type="warning"
                  message="请先配置至少一个 Provider，再选择激活模型。"
                />
              ) : (
                <Form
                  form={activeForm}
                  layout="vertical"
                  className={styles.settingsForm}
                  onFinish={(values) => void saveActiveModels(values)}
                  onValuesChange={(changed) => {
                    if ('default_provider_id' in changed) {
                      activeForm.setFieldValue('default_model_id', undefined);
                    }
                  }}
                >
                  <Form.Item
                    label="默认 Provider"
                    name="default_provider_id"
                    rules={[{ required: true, message: '请选择默认 Provider' }]}
                  >
                    <Select options={configuredProviders.map((provider) => ({
                      value: provider.id,
                      label: provider.name,
                    }))} />
                  </Form.Item>
                  <Form.Item
                    label="默认模型"
                    name="default_model_id"
                    rules={[{ required: true, message: '请选择默认模型' }]}
                  >
                    <Select disabled={!defaultProviderId} options={defaultModelOptions} />
                  </Form.Item>
                  <Form.Item label="轻量模型（可选）" name="light_selection">
                    <Select allowClear options={allModelOptions} placeholder="未选择时使用默认模型" />
                  </Form.Item>
                  <div className={styles.formActions}>
                    <Button type="primary" htmlType="submit" loading={savingModels}>
                      保存激活模型
                    </Button>
                  </div>
                </Form>
              )}
            </section>

            <section className={styles.section}>
              <div className={styles.sectionHeading}>
                <span><SettingOutlined /></span>
                <div>
                  <h3>运行参数</h3>
                  <p>控制单轮最大迭代次数和模型调用重试策略。</p>
                </div>
              </div>
              <Form
                form={runtimeForm}
                layout="vertical"
                className={styles.settingsForm}
                onFinish={(values) => void saveRuntimeSettings(values)}
              >
                <Form.Item label="最大 ReAct 迭代次数" name="react_max_iters">
                  <InputNumber min={1} max={200} precision={0} />
                </Form.Item>
                <Form.Item label="启用 LLM 重试" name="llm_retry_enabled" valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Form.Item label="最大重试次数" name="llm_max_retries">
                  <InputNumber min={0} max={20} precision={0} />
                </Form.Item>
                <div className={styles.formActions}>
                  <Button type="primary" htmlType="submit" loading={savingRuntime}>
                    保存运行参数
                  </Button>
                </div>
              </Form>
            </section>
          </div>
        ) : null}
      </main>
    </div>
  );
}
