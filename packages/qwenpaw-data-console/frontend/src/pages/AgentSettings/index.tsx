import {
  CheckCircleFilled,
  CloseCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Input, Select } from '@agentscope-ai/design';
import {
  Alert,
  Form,
  InputNumber,
  Modal,
  Popconfirm,
  Spin,
  Switch,
  Tag,
  Tabs,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { dataConsoleApi } from '@/features/console/api/client';
import {
  activeModelSaveMode,
  hasSemanticModelSettings,
  saveActiveModelDraft,
  type ActiveModelDraft,
} from '@/features/console/model/chat/activeModelSettings';
import {
  isToolConfirmationLevel,
  normalizeRuntimeSettings,
  saveRuntimeSettingsDraft,
} from '@/features/console/model/chat/toolConfirmationSettings';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader/index';
import SecretInput from '@/features/console/components/SecretInput';
import ContextCompressionSettings from './ContextCompressionSettings';
import type { ModelProvider, ProviderModel, RuntimeSettings } from '@/features/console/api/types';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';
import DataBridgeModelSettings from './DataBridgeModelSettings';
import { useDataBridgeModelSettings } from './useDataBridgeModelSettings';
import { modelConfigApi } from '@/features/console/api/modelConfig';
import { getHostAuthContext } from '@/services/auth';
import { useLocation, useNavigate } from 'react-router-dom';
import { DEFAULT_EMBEDDING_DIM } from './embeddingModels';
import { getAgentSettingsReturnPath } from '@/shared/navigation/sessionNavigation';

interface ProviderCardProps {
  provider: ModelProvider;
  onChange: (provider: ModelProvider) => void;
}

interface ProviderModelsProps {
  provider: ModelProvider;
  onChange: (providerId: string, models: ProviderModel[]) => void;
}

function isExtraModelEditor(editing: ProviderModel | 'extra' | null): boolean {
  return editing === 'extra' || (typeof editing === 'object' && editing?.source === 'extra');
}

function supportsThinking(provider: ModelProvider, model?: ProviderModel): boolean {
  return (model?.chat_model ?? provider.chat_model) === 'DashScopeChatModel';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function modelSelection(providerId: string | null, modelId: string | null) {
  return providerId && modelId ? `${providerId}/${modelId}` : undefined;
}

function ProviderCard({ provider, onChange }: ProviderCardProps) {
  const { t } = useAppI18n();
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  const save = async () => {
    const trimmedKey = apiKey.trim();
    if (!provider.configured && !trimmedKey) {
      message.warning(t('agent.apiKeyRequired'));
      return;
    }
    setSaving(true);
    try {
      const { provider: updated } = await dataConsoleApi.updateModelProvider(provider.id, {
        ...(trimmedKey ? { api_key: trimmedKey } : {}),
        base_url: baseUrl.trim() || null,
      });
      setApiKey('');
      setBaseUrl(updated.base_url);
      onChange(updated);
      message.success(t('agent.providerSaved', { name: provider.name }));
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
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
      message.success(t('agent.credentialsCleared', { name: provider.name }));
    } catch (error) {
      message.error(t('agent.clearFailed', { message: errorMessage(error) }));
    } finally {
      setClearing(false);
    }
  };

  return (
    <article className={styles.providerCard}>
      <header>
        <div className={styles.providerIdentity}>
          <strong>{provider.name}</strong>
          <span>{provider.chat_model}</span>
        </div>
        <Tag
          className={styles.providerStatus}
          color={provider.configured ? 'success' : 'default'}
          icon={provider.configured ? <CheckCircleFilled /> : undefined}
        >
          {provider.configured ? t('common.configured') : t('common.notConfigured')}
        </Tag>
      </header>

      <div className={styles.providerFields}>
        <label>
          <span>API Key</span>
          <SecretInput
            autoComplete="new-password"
            value={apiKey}
            placeholder={
              provider.configured
                ? t('agent.savedSecretPlaceholder', { masked: provider.api_key_masked })
                : t('agent.apiKeyPlaceholder', { prefix: provider.api_key_prefix })
            }
            onChange={(event) => setApiKey(event.target.value)}
          />
        </label>
        <label>
          <span>Base URL</span>
          <Input
            value={baseUrl}
            placeholder={provider.catalog_base_url || t('agent.baseUrlPlaceholder')}
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
            {t('agent.clearCredentials')}
          </Button>
        ) : (
          <span />
        )}
        <Button type="primary" loading={saving} onClick={() => void save()}>
          {t('agent.saveProvider')}
        </Button>
      </footer>
    </article>
  );
}

function ProviderModels({ provider, onChange }: ProviderModelsProps) {
  const { t } = useAppI18n();
  const [editing, setEditing] = useState<ProviderModel | 'extra' | null>(null);
  const [modelId, setModelId] = useState('');
  const [name, setName] = useState('');
  const [contextSize, setContextSize] = useState<number | null>(null);
  const [generateKwargs, setGenerateKwargs] = useState('{}');
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [thinkingId, setThinkingId] = useState<string | null>(null);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const invalidContextSize =
    contextSize !== null && (!Number.isSafeInteger(contextSize) || contextSize <= 0);

  const closeEditor = () => {
    if (saving) return;
    setEditing(null);
  };

  const openEditor = (model: ProviderModel | 'extra') => {
    setEditing(model);
    if (model === 'extra') {
      setModelId('');
      setName('');
      setContextSize(null);
      setThinkingEnabled(false);
      setGenerateKwargs('{}');
      return;
    }
    setModelId(model.id);
    setName(model.name);
    setContextSize(model.context_size);
    setThinkingEnabled(model.thinking_enabled ?? false);
    setGenerateKwargs(JSON.stringify(model.generate_kwargs ?? {}, null, 2));
  };

  const saveModel = async () => {
    const trimmedModelId = modelId.trim();
    const source = isExtraModelEditor(editing) ? 'extra' : 'override';
    if (!trimmedModelId) {
      message.warning(t('agent.modelIdRequired'));
      return;
    }
    if (source === 'extra' && !name.trim()) {
      message.warning(t('agent.modelNameRequired'));
      return;
    }

    if (
      source === 'extra' &&
      (contextSize === null || !Number.isSafeInteger(contextSize) || contextSize <= 0)
    ) {
      message.warning(t('contextUsage.sizeRequired'));
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
      message.error(t('agent.invalidGenerateParams'));
      return;
    }

    setSaving(true);
    try {
      const { model } = await dataConsoleApi.upsertProviderModel(provider.id, trimmedModelId, {
        source,
        name: source === 'extra' ? name.trim() : null,
        ...(source === 'extra' ? { context_size: contextSize } : {}),
        thinking_enabled: supportsThinking(
          provider,
          editing !== null && typeof editing === 'object' ? editing : undefined,
        )
          ? thinkingEnabled
          : null,
        generate_kwargs: parsedKwargs,
      });
      const nextModels = provider.models.some((item) => item.id === model.id)
        ? provider.models.map((item) => (item.id === model.id ? model : item))
        : [...provider.models, model];
      onChange(provider.id, nextModels);
      setEditing(null);
      message.success(t('agent.modelSaved', { name: model.name }));
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
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
      message.success(
        model.source === 'override' ? t('agent.modelRestored') : t('agent.extraModelDeleted'),
      );
    } catch (error) {
      message.error(t('common.operationFailed', { message: errorMessage(error) }));
    } finally {
      setDeletingId(null);
    }
  };

  const updateThinking = async (model: ProviderModel, enabled: boolean) => {
    setThinkingId(model.id);
    try {
      const source = model.source === 'extra' ? 'extra' : 'override';
      const { model: updated } = await dataConsoleApi.upsertProviderModel(provider.id, model.id, {
        source,
        name: source === 'extra' ? model.name : null,
        ...(source === 'extra' ? { context_size: model.context_size } : {}),
        thinking_enabled: enabled,
        generate_kwargs: model.generate_kwargs,
      });
      onChange(
        provider.id,
        provider.models.map((item) => (item.id === updated.id ? updated : item)),
      );
      message.success(t('agent.thinkingUpdated', { name: model.name }));
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
    } finally {
      setThinkingId(null);
    }
  };

  return (
    <article className={styles.modelProviderCard}>
      <header>
        <div>
          <strong>{provider.name}</strong>
          <span>{t('agent.availableModels', { count: provider.models.length })}</span>
        </div>
        <Button size="small" icon={<PlusOutlined />} onClick={() => openEditor('extra')}>
          {t('agent.addModel')}
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
                  {model.source === 'catalog'
                    ? t('agent.builtin')
                    : model.source === 'extra'
                      ? t('agent.extra')
                      : t('agent.overridden')}
                </Tag>
              </div>
            </div>
            <div className={styles.modelActions}>
              {supportsThinking(provider, model) ? (
                <label className={styles.modelThinking}>
                  <span>{t('agent.thinking')}</span>
                  <Switch
                    size="small"
                    checked={model.thinking_enabled ?? false}
                    loading={thinkingId === model.id}
                    onChange={(enabled) => void updateThinking(model, enabled)}
                  />
                </label>
              ) : null}
              <div className={styles.modelActionButtons}>
                <Button size="small" type="text" onClick={() => openEditor(model)}>
                  {t('agent.advancedConfig')}
                </Button>
                {model.source !== 'catalog' ? (
                  <Popconfirm
                    title={
                      model.source === 'override'
                        ? t('agent.restoreTitle')
                        : t('agent.deleteExtraTitle')
                    }
                    okText={t('common.confirm')}
                    cancelText={t('common.cancel')}
                    onConfirm={() => void deleteModel(model)}
                  >
                    <Button danger size="small" type="text" loading={deletingId === model.id}>
                      {model.source === 'override' ? t('agent.restoreBuiltin') : t('common.delete')}
                    </Button>
                  </Popconfirm>
                ) : null}
              </div>
            </div>
          </div>
        ))}
      </div>

      <Modal
        open={editing !== null}
        title={
          editing === 'extra'
            ? t('agent.addModelFor', { name: provider.name })
            : t('agent.editModel')
        }
        okText={t('common.save')}
        cancelText={t('common.cancel')}
        confirmLoading={saving}
        onCancel={closeEditor}
        onOk={() => void saveModel()}
      >
        <div className={styles.modelEditor}>
          <label>
            <span>{t('agent.modelId')}</span>
            <Input
              value={modelId}
              disabled={editing !== 'extra'}
              placeholder={t('agent.modelIdPlaceholder')}
              onChange={(event) => setModelId(event.target.value)}
            />
          </label>
          {isExtraModelEditor(editing) ? (
            <label>
              <span>{t('agent.modelName')}</span>
              <Input
                value={name}
                placeholder={t('agent.modelNamePlaceholder')}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
          ) : null}
          {isExtraModelEditor(editing) ? (
            <label>
              <span>{t('contextUsage.size')}</span>
              <InputNumber
                className={styles.contextSizeInput}
                aria-label={t('contextUsage.size')}
                value={contextSize}
                min={1}
                max={Number.MAX_SAFE_INTEGER}
                step={1024}
                precision={0}
                status={invalidContextSize ? 'error' : undefined}
                disabled={saving}
                onChange={setContextSize}
              />
              <span className={styles.contextSizeHint}>{t('contextUsage.sizeHint')}</span>
            </label>
          ) : null}
          {supportsThinking(
            provider,
            editing !== null && typeof editing === 'object' ? editing : undefined,
          ) ? (
            <label className={styles.modelEditorSwitch}>
              <span>{t('agent.thinking')}</span>
              <Switch checked={thinkingEnabled} onChange={setThinkingEnabled} />
            </label>
          ) : null}
          <label>
            <span>{t('agent.generateParams')}</span>
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

type SettingsSection = 'providers' | 'active' | 'runtime';
type SectionStatus = 'loading' | 'ready' | Error;
const initialStatus = { providers: 'loading', active: 'loading', runtime: 'loading' } as const;

export default function AgentSettingsPage() {
  const { t } = useAppI18n();
  const [activeForm] = Form.useForm<ActiveModelDraft>();
  const [runtimeForm] = Form.useForm<RuntimeSettings>();
  const toolTimeoutEnabled = Form.useWatch('tool_timeout_enabled', runtimeForm);
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [status, setStatus] = useState<Record<SettingsSection, SectionStatus>>(initialStatus);
  const attempts = useRef({ providers: 0, active: 0, runtime: 0 });
  const [savingModels, setSavingModels] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [confirmationAvailable, setConfirmationAvailable] = useState(false);
  const runtimeLoadedScope = useRef<{ generation: number; revision: number } | null>(null);
  const runtimeSavingScope = useRef<object | null>(null);
  const semanticEnabled = hasSemanticModelSettings(
    window.QWENPAW_DATA_CONSOLE?.capabilities?.contextConsole,
  );
  const cm = useDataBridgeModelSettings(activeForm, providers, semanticEnabled);
  const saveMode = activeModelSaveMode(semanticEnabled, cm);
  const canSubmitModels =
    !savingModels &&
    cm.auth.ready &&
    status.providers === 'ready' &&
    status.active === 'ready' &&
    saveMode !== 'blocked';
  const navigate = useNavigate();
  const location = useLocation();
  const returnPath = getAgentSettingsReturnPath(location.search);
  const alive = useRef(0);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [contextSettings, setContextSettings] = useState<RuntimeSettings | null>(null);

  const load = useCallback(
    async (section: SettingsSection) => {
      const generation = alive.current;
      const revision = getHostAuthContext().revision;
      const attempt = ++attempts.current[section];
      const current = () =>
        generation === alive.current &&
        revision === getHostAuthContext().revision &&
        attempt === attempts.current[section];
      setStatus((old) => ({ ...old, [section]: 'loading' }));
      try {
        if (section === 'providers') {
          const { providers } = await dataConsoleApi.getModelProviders();
          if (!current()) return;
          setProviders(providers);
        } else if (section === 'active') {
          const { active_models: active } = await dataConsoleApi.getActiveModels();
          if (!current()) return;
          activeForm.setFieldsValue({
            default_provider_id: active.default_provider_id ?? undefined,
            default_model_id: active.default_model_id ?? undefined,
            light_selection: modelSelection(active.light_provider_id, active.light_model_id),
          });
        } else {
          runtimeLoadedScope.current = null;
          const runtimeResponse = await dataConsoleApi.getRuntimeSettings();
          if (!current()) return;
          runtimeLoadedScope.current = { generation, revision };
          setConfirmationAvailable(
            isToolConfirmationLevel(runtimeResponse.runtime_settings.tool_confirmation_level),
          );
          runtimeForm.setFieldsValue(normalizeRuntimeSettings(runtimeResponse.runtime_settings));
          setContextSettings(runtimeResponse.runtime_settings);
        }
        setStatus((old) => ({ ...old, [section]: 'ready' }));
      } catch (error) {
        if (current()) setStatus((old) => ({ ...old, [section]: new Error(errorMessage(error)) }));
      }
    },
    [activeForm, runtimeForm],
  );

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('agent.title')} · QwenPaw Data`;
    setSavingModels(false);
    setSavingRuntime(false);
    runtimeLoadedScope.current = null;
    runtimeSavingScope.current = null;
    setSaveError(null);
    setStatus(initialStatus);
    setProviders([]);
    setContextSettings(null);
    if (cm.auth.ready) {
      void load('providers');
      void load('active');
      void load('runtime');
    }
    return () => {
      alive.current += 1;
      document.title = previousTitle;
    };
  }, [load, t, cm.auth.ready, cm.auth.revision]);

  const configuredProviders = useMemo(
    () => providers.filter((provider) => provider.configured),
    [providers],
  );
  const defaultProviderId = Form.useWatch('default_provider_id', activeForm);
  const defaultModelOptions = useMemo(
    () =>
      providers
        .find((provider) => provider.id === defaultProviderId)
        ?.models.map((model) => ({ value: model.id, label: model.name })) ?? [],
    [defaultProviderId, providers],
  );
  const allModelOptions = useMemo(
    () =>
      configuredProviders.flatMap((provider) =>
        provider.models.map((model) => ({
          value: `${provider.id}/${model.id}`,
          label: `${provider.name} · ${model.name}`,
        })),
      ),
    [configuredProviders],
  );

  const updateProvider = useCallback(
    (updated: ModelProvider) => {
      cm.clearResults();
      void cm.load(false);
      setProviders((current) =>
        current.map((provider) => (provider.id === updated.id ? updated : provider)),
      );
      if (!updated.configured) {
        if (activeForm.getFieldValue('default_provider_id') === updated.id) {
          activeForm.setFieldsValue({
            default_provider_id: undefined,
            default_model_id: undefined,
          });
        }
        if (activeForm.getFieldValue('cm_provider_id') === updated.id)
          activeForm.setFieldValue('cm_provider_id', undefined);
        if (activeForm.getFieldValue('embedding_provider_id') === updated.id)
          activeForm.setFieldValue('embedding_provider_id', undefined);
        const lightSelection = activeForm.getFieldValue('light_selection');
        if (lightSelection?.startsWith(`${updated.id}/`)) {
          activeForm.setFieldValue('light_selection', undefined);
        }
      }
    },
    [activeForm, cm],
  );

  const updateProviderModels = useCallback((providerId: string, models: ProviderModel[]) => {
    setProviders((current) =>
      current.map((provider) => (provider.id === providerId ? { ...provider, models } : provider)),
    );
  }, []);

  const saveActiveModels = async (values: ActiveModelDraft) => {
    if (!canSubmitModels) return;
    const generation = alive.current;
    const revision = cm.auth.revision;
    const current = () =>
      generation === alive.current && revision === getHostAuthContext().revision;
    setSavingModels(true);
    setSaveError(null);
    try {
      const failures = await saveActiveModelDraft(saveMode, values, providers, {
        updateActiveModels: dataConsoleApi.updateActiveModels,
        updateLLM: modelConfigApi.updateLLM,
        updateEmbedding: modelConfigApi.updateEmbedding,
      });
      if (!current()) return;
      if (failures.length) {
        const items = failures
          .map(
            ({ section, reason }) =>
              `${section === 'session' ? t('agent.activeModels') : section}: ${errorMessage(reason)}`,
          )
          .join('；');
        setSaveError(t('agent.cm.saveFailed', { items }));
      } else message.success(t('agent.activeModelsSaved'));
      if (semanticEnabled) await cm.load(false);
    } catch (error) {
      if (current()) setSaveError(errorMessage(error));
    } finally {
      if (current()) setSavingModels(false);
    }
  };

  const saveRuntimeSettings = async (values: RuntimeSettings) => {
    const scope = runtimeLoadedScope.current;
    const current = () =>
      Boolean(
        scope &&
        scope.generation === alive.current &&
        scope.revision === getHostAuthContext().revision &&
        getHostAuthContext().ready,
      );
    if (!current() || runtimeSavingScope.current) return;
    runtimeSavingScope.current = scope;
    setSavingRuntime(true);
    try {
      await saveRuntimeSettingsDraft(values, confirmationAvailable, {
        isCurrent: current,
        save: dataConsoleApi.updateRuntimeSettings,
        onSaved: (saved) => {
          runtimeForm.setFieldsValue(saved);
          setConfirmationAvailable(isToolConfirmationLevel(saved.tool_confirmation_level));
          message.success(t('agent.runtimeSettingsSaved'));
        },
      });
    } catch (error) {
      if (current()) message.error(t('common.saveFailed', { message: errorMessage(error) }));
    } finally {
      if (runtimeSavingScope.current === scope) runtimeSavingScope.current = null;
      if (current()) setSavingRuntime(false);
    }
  };

  const sectionNotice = (section: SettingsSection) => {
    const state = status[section];
    if (state === 'ready') return null;
    return state === 'loading' ? (
      <Spin aria-label={t('agent.loading')} />
    ) : (
      <Alert
        type="error"
        showIcon
        message={t('agent.loadFailed')}
        description={state.message}
        action={
          <Button size="small" disabled={false} onClick={() => void load(section)}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  };

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('agent.title')} />

      <main className={styles.main}>
        {returnPath ? (
          <Button className={styles.returnButton} onClick={() => navigate(returnPath)}>
            {t('agent.cm.back')}
          </Button>
        ) : null}
        <Tabs
          className={styles.tabs}
          defaultActiveKey="models"
          items={[
            {
              key: 'models',
              label: t('agent.modelConfigTab'),
              forceRender: true,
              children: (
                <div className={styles.sections}>
                  <section
                    className={`${styles.section} ${styles.modelConfigurationSection} qwenpaw-data-card-primary`}
                  >
                    {status.providers !== 'ready' ? (
                      sectionNotice('providers')
                    ) : (
                      <div className={styles.modelSetupGrid}>
                        <div className={styles.modelSetupColumn}>
                          <div className={styles.sectionHeading}>
                            <h3>{t('agent.providerCredentials')}</h3>
                            <p>{t('agent.providerCredentialsDescription')}</p>
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
                        </div>
                        <div className={`${styles.modelSetupColumn} ${styles.modelListColumn}`}>
                          <div
                            className={`${styles.sectionHeading} ${styles.sectionHeadingWithAction}`}
                          >
                            <h3>{t('agent.providerModels')}</h3>
                            <p>{t('agent.providerModelsDescription')}</p>
                            <Button
                              className={styles.sectionHeadingAction}
                              aria-label={t('common.refresh')}
                              title={t('common.refresh')}
                              icon={<ReloadOutlined />}
                              onClick={() => void load('providers')}
                            />
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
                        </div>
                      </div>
                    )}
                  </section>

                  {status.providers === 'ready' && (
                    <section className={`${styles.section} qwenpaw-data-card-primary`}>
                      {!configuredProviders.length ? (
                        <Alert
                          showIcon
                          type="warning"
                          message={t('agent.configureProviderFirst')}
                        />
                      ) : (
                        <Form
                          form={activeForm}
                          layout="vertical"
                          className={`${styles.settingsForm} ${styles.activeModelForm}`}
                          disabled={savingModels}
                          onFinish={(values) => void saveActiveModels(values)}
                          onValuesChange={(changed) => {
                            cm.clearResults();
                            if ('cm_provider_id' in changed)
                              activeForm.setFieldValue('cm_model_id', undefined);
                            if ('embedding_model_id' in changed)
                              activeForm.setFieldValue('embedding_dim', DEFAULT_EMBEDDING_DIM);
                            if ('default_provider_id' in changed) {
                              activeForm.setFieldValue('default_model_id', undefined);
                            }
                          }}
                        >
                          <div
                            className={`${styles.sessionModelSettings} ${semanticEnabled ? '' : styles.fullWidth}`}
                          >
                            <div className={styles.sectionHeading}>
                              <h3>{t('agent.activeModels')}</h3>
                              <p>{t('agent.activeModelsDescription')}</p>
                            </div>
                            {sectionNotice('active')}
                            <Form.Item
                              label={t('agent.defaultProvider')}
                              name="default_provider_id"
                              rules={[
                                { required: true, message: t('agent.defaultProviderRequired') },
                              ]}
                            >
                              <Select
                                options={configuredProviders.map((provider) => ({
                                  value: provider.id,
                                  label: provider.name,
                                }))}
                              />
                            </Form.Item>
                            <Form.Item
                              label={t('agent.defaultModel')}
                              name="default_model_id"
                              rules={[{ required: true, message: t('agent.defaultModelRequired') }]}
                            >
                              <Select disabled={!defaultProviderId} options={defaultModelOptions} />
                            </Form.Item>
                            <Form.Item label={t('agent.lightModel')} name="light_selection">
                              <Select
                                allowClear
                                options={allModelOptions}
                                placeholder={t('agent.lightModelPlaceholder')}
                              />
                            </Form.Item>
                          </div>
                          {semanticEnabled && (
                            <DataBridgeModelSettings
                              form={activeForm}
                              providers={providers}
                              settings={cm}
                              saving={savingModels}
                            />
                          )}
                          {saveError ? (
                            <Alert
                              className={styles.fullWidth}
                              type="error"
                              showIcon
                              message={saveError}
                            />
                          ) : null}
                          <div className={`${styles.formActions} ${styles.fullWidth}`}>
                            <Button
                              type="primary"
                              htmlType="submit"
                              loading={savingModels}
                              disabled={!canSubmitModels}
                            >
                              {t('agent.saveActiveModels')}
                            </Button>
                          </div>
                        </Form>
                      )}
                    </section>
                  )}
                </div>
              ),
            },
            {
              key: 'agent',
              label: t('agent.agentConfigTab'),
              forceRender: true,
              children: (
                <div className={styles.sections}>
                  <section className={`${styles.section} qwenpaw-data-card-primary`}>
                    <div className={styles.sectionHeading}>
                      <h3>{t('agent.runtimeSettings')}</h3>
                      <p>{t('agent.runtimeSettingsDescription')}</p>
                    </div>
                    {status.runtime !== 'ready' ? (
                      sectionNotice('runtime')
                    ) : (
                      <Form
                        form={runtimeForm}
                        layout="vertical"
                        className={`${styles.settingsForm} ${styles.runtimeForm}`}
                        initialValues={{ tool_timeout_enabled: true, tool_timeout_seconds: 1800 }}
                        disabled={savingRuntime}
                        onFinish={(values) => void saveRuntimeSettings(values)}
                      >
                        <Form.Item
                          className={styles.fullWidth}
                          label={t('toolConfirmation.setting')}
                          name="tool_confirmation_level"
                          extra={t(
                            confirmationAvailable
                              ? 'toolConfirmation.settingHint'
                              : 'toolConfirmation.settingUnavailable',
                          )}
                        >
                          <Select
                            disabled={savingRuntime || !confirmationAvailable}
                            placeholder={t('toolConfirmation.settingUnavailable')}
                            options={(['off', 'auto', 'all'] as const).map((value) => ({
                              value,
                              label: t(`toolConfirmation.level.${value}`),
                            }))}
                          />
                        </Form.Item>
                        <Form.Item
                          className={styles.fullWidth}
                          label={t('agent.reactMaxIterations')}
                          name="react_max_iters"
                        >
                          <InputNumber min={1} precision={0} />
                        </Form.Item>
                        <Form.Item
                          label={t('agent.enableLlmRetry')}
                          name="llm_retry_enabled"
                          valuePropName="checked"
                        >
                          <Switch />
                        </Form.Item>
                        <Form.Item label={t('agent.maxRetries')} name="llm_max_retries">
                          <InputNumber min={0} max={20} precision={0} />
                        </Form.Item>
                        <Form.Item
                          label={t('agent.enableToolTimeout')}
                          name="tool_timeout_enabled"
                          valuePropName="checked"
                        >
                          <Switch />
                        </Form.Item>
                        <Form.Item
                          label={t('agent.toolTimeoutSeconds')}
                          name="tool_timeout_seconds"
                          rules={[
                            {
                              required: true,
                              type: 'number',
                              min: 1,
                              message: t('agent.toolTimeoutInvalid'),
                            },
                          ]}
                          extra={t('agent.toolTimeoutDescription')}
                        >
                          <InputNumber min={1} disabled={savingRuntime || !toolTimeoutEnabled} />
                        </Form.Item>
                        <div className={`${styles.formActions} ${styles.fullWidth}`}>
                          <Button type="primary" htmlType="submit" loading={savingRuntime}>
                            {t('agent.saveRuntimeSettings')}
                          </Button>
                        </div>
                      </Form>
                    )}
                  </section>
                  {status.runtime === 'ready' && contextSettings ? (
                    <ContextCompressionSettings initialSettings={contextSettings} />
                  ) : null}
                </div>
              ),
            },
          ]}
        />
      </main>
    </div>
  );
}
