import { CloudServerOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Input, Select } from '@agentscope-ai/design';
import {
  Alert,
  Empty,
  Form,
  InputNumber,
  Popconfirm,
  Segmented,
  Spin,
  Switch,
  Table,
  Tag,
  message,
  type TableColumnsType,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  mcpApi,
  type McpConfig,
  type McpItem,
  type McpTool,
  type McpWriteBody,
} from '@/features/console/api/mcp';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import RouteDrawer from '@/features/console/components/RouteDrawer';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

interface Pair {
  key: string;
  value: string;
}

interface McpFormValues {
  name: string;
  description: string;
  enabled: boolean;
  type: 'http_mcp' | 'stdio_mcp';
  url: string;
  command: string;
  argsText: string;
  cwd: string;
  timeout?: number | null;
  execution_timeout?: number | null;
  is_stateful: boolean;
  headers: Pair[];
  env: Pair[];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function jsonErrorCode(error: unknown): string {
  return error instanceof Error ? error.message : 'invalid_config';
}

function pairsFromRecord(record?: Record<string, string> | null): Pair[] {
  return Object.entries(record ?? {}).map(([key, value]) => ({ key, value }));
}

function recordFromPairs(pairs: Pair[] | undefined): Record<string, string> {
  const record: Record<string, string> = {};
  for (const pair of pairs ?? []) {
    const key = pair.key.trim();
    if (!key) continue;
    record[key] = pair.value;
  }
  return record;
}

function parseArgs(text: string): string[] | null {
  const args = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  return args.length ? args : null;
}

function emptyForm(): McpFormValues {
  return {
    name: '',
    description: '',
    enabled: true,
    type: 'http_mcp',
    url: '',
    command: '',
    argsText: '',
    cwd: '',
    timeout: 30,
    execution_timeout: null,
    is_stateful: false,
    headers: [],
    env: [],
  };
}

function formFromMcp(mcp: McpItem): McpFormValues {
  const config = mcp.mcp_config;
  if (config.type === 'stdio_mcp') {
    return {
      ...emptyForm(),
      name: mcp.name,
      description: mcp.description,
      enabled: mcp.enabled,
      type: 'stdio_mcp',
      command: config.command,
      argsText: (config.args ?? []).join('\n'),
      cwd: config.cwd ?? '',
      execution_timeout: mcp.execution_timeout,
      is_stateful: true,
      env: pairsFromRecord(config.env),
    };
  }
  return {
    ...emptyForm(),
    name: mcp.name,
    description: mcp.description,
    enabled: mcp.enabled,
    type: 'http_mcp',
    url: config.url,
    timeout: config.timeout ?? 30,
    execution_timeout: mcp.execution_timeout,
    is_stateful: mcp.is_stateful,
    headers: pairsFromRecord(config.headers),
  };
}

function bodyFromForm(values: McpFormValues, source: 'builtin' | 'custom'): McpWriteBody {
  const mcp_config: McpConfig =
    values.type === 'stdio_mcp'
      ? {
          type: 'stdio_mcp',
          command: values.command.trim(),
          args: parseArgs(values.argsText),
          env: recordFromPairs(values.env),
          cwd: values.cwd.trim() || null,
        }
      : {
          type: 'http_mcp',
          url: values.url.trim(),
          headers: recordFromPairs(values.headers),
          timeout: values.timeout ?? 30,
        };
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    enabled: values.enabled,
    source,
    is_stateful: values.type === 'stdio_mcp' ? true : values.is_stateful,
    mcp_config,
    enable_tools: null,
    disable_tools: null,
    execution_timeout: values.execution_timeout ?? null,
  };
}

function endpointLabel(mcp: McpItem): string {
  const config = mcp.mcp_config;
  return config.type === 'stdio_mcp' ? config.command : config.url;
}

function prettyJson(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function asStringRecord(value: unknown): Record<string, string> | null {
  if (value == null) return null;
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid_config');
  }
  const record: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry !== 'string') {
      throw new Error('invalid_config');
    }
    record[key] = entry;
  }
  return record;
}

function jsonFromConfig(config: McpConfig): string {
  if (config.type === 'stdio_mcp') {
    const payload: Record<string, unknown> = { command: config.command };
    if (config.args?.length) payload.args = config.args;
    if (config.env && Object.keys(config.env).length) payload.env = config.env;
    if (config.cwd) payload.cwd = config.cwd;
    return prettyJson(payload);
  }
  const payload: Record<string, unknown> = { url: config.url };
  if (config.headers && Object.keys(config.headers).length) {
    payload.headers = config.headers;
  }
  if (config.timeout != null) payload.timeout = config.timeout;
  return prettyJson(payload);
}

function emptyJson(): string {
  return prettyJson({
    mcpServers: {
      example: {
        url: 'https://example.com/mcp',
      },
    },
  });
}

function parseJsonObject(text: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error('invalid_json');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('invalid_config');
  }
  return parsed as Record<string, unknown>;
}

function configFromServerObject(record: Record<string, unknown>): McpConfig {
  const url = typeof record.url === 'string' ? record.url.trim() : '';
  const command = typeof record.command === 'string' ? record.command.trim() : '';
  if (Boolean(url) === Boolean(command)) {
    throw new Error('invalid_config');
  }
  if (url) {
    return {
      type: 'http_mcp',
      url,
      headers: asStringRecord(record.headers) ?? {},
      timeout: typeof record.timeout === 'number' ? record.timeout : 30,
    };
  }
  if (record.args != null) {
    if (!Array.isArray(record.args) || record.args.some((item) => typeof item !== 'string')) {
      throw new Error('invalid_config');
    }
  }
  return {
    type: 'stdio_mcp',
    command,
    args: Array.isArray(record.args) ? record.args : null,
    env: asStringRecord(record.env),
    cwd: typeof record.cwd === 'string' && record.cwd.trim() ? record.cwd : null,
  };
}

interface ParsedServer {
  name?: string;
  config: McpConfig;
}

function parseEditorPayload(text: string): ParsedServer[] {
  const record = parseJsonObject(text);
  if (record.mcpServers == null) {
    return [{ config: configFromServerObject(record) }];
  }
  if (typeof record.mcpServers !== 'object' || Array.isArray(record.mcpServers)) {
    throw new Error('invalid_config');
  }
  const servers = record.mcpServers as Record<string, unknown>;
  const names = Object.keys(servers);
  if (!names.length) {
    throw new Error('empty_bundle');
  }
  return names.map((name) => {
    const server = servers[name];
    if (!server || typeof server !== 'object' || Array.isArray(server)) {
      throw new Error('invalid_config');
    }
    return { name, config: configFromServerObject(server as Record<string, unknown>) };
  });
}

function writeBodyFromServer(
  name: string,
  config: McpConfig,
  source: 'builtin' | 'custom',
  host: Pick<
    McpWriteBody,
    'description' | 'enabled' | 'enable_tools' | 'disable_tools' | 'execution_timeout'
  >,
): McpWriteBody {
  return {
    name,
    description: host.description,
    enabled: host.enabled,
    source,
    is_stateful: config.type === 'stdio_mcp',
    mcp_config: config,
    enable_tools: host.enable_tools,
    disable_tools: host.disable_tools,
    execution_timeout: host.execution_timeout,
  };
}

function formValuesFromConfig(config: McpConfig): Partial<McpFormValues> {
  if (config.type === 'stdio_mcp') {
    return {
      type: 'stdio_mcp',
      command: config.command,
      argsText: (config.args ?? []).join('\n'),
      cwd: config.cwd ?? '',
      is_stateful: true,
      env: pairsFromRecord(config.env),
    };
  }
  return {
    type: 'http_mcp',
    url: config.url,
    timeout: config.timeout ?? 30,
    is_stateful: false,
    headers: pairsFromRecord(config.headers),
  };
}

export default function McpSettingsPage() {
  const { t } = useAppI18n();
  const [form] = Form.useForm<McpFormValues>();
  const transport = Form.useWatch('type', form);
  const [items, setItems] = useState<McpItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const refreshInFlight = useRef(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [editor, setEditor] = useState<McpItem | 'create' | null>(null);
  const [editorMode, setEditorMode] = useState<'form' | 'json'>('json');
  const [jsonText, setJsonText] = useState(emptyJson());
  const [saving, setSaving] = useState(false);
  const [toolsMcp, setToolsMcp] = useState<McpItem | null>(null);
  const [tools, setTools] = useState<McpTool[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [allTools, setAllTools] = useState(true);
  const [sourceFilter, setSourceFilter] = useState<'builtin' | 'custom'>('builtin');

  const fetchItems = useCallback(async () => {
    const { mcps } = await mcpApi.list();
    setItems(mcps);
    setLoadError(null);
  }, []);

  const refreshItems = useCallback(async () => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    setRefreshing(true);
    try {
      await fetchItems();
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      refreshInFlight.current = false;
      setRefreshing(false);
    }
  }, [fetchItems]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('mcp.title')} · QwenPaw Data`;
    void fetchItems()
      .catch((error) => setLoadError(errorMessage(error)))
      .finally(() => setLoading(false));
    return () => {
      document.title = previousTitle;
    };
  }, [fetchItems, t]);

  const builtins = useMemo(() => items.filter((item) => item.source === 'builtin'), [items]);
  const customs = useMemo(() => items.filter((item) => item.source !== 'builtin'), [items]);

  const runAction = useCallback(
    async (key: string, action: () => Promise<unknown>, success: string) => {
      setPendingAction(key);
      try {
        await action();
        await fetchItems();
        message.success(success);
      } catch (error) {
        message.error(t('common.operationFailed', { message: errorMessage(error) }));
      } finally {
        setPendingAction(null);
      }
    },
    [fetchItems, t],
  );

  const openCreate = () => {
    setEditor('create');
    setEditorMode('json');
    form.setFieldsValue(emptyForm());
    setJsonText(emptyJson());
  };

  const openEdit = (mcp: McpItem) => {
    setEditor(mcp);
    setEditorMode('json');
    form.setFieldsValue(formFromMcp(mcp));
    setJsonText(jsonFromConfig(mcp.mcp_config));
  };

  const switchEditorMode = (mode: 'form' | 'json') => {
    if (mode === editorMode) return;
    if (mode === 'json') {
      const values = form.getFieldsValue(true);
      setJsonText(jsonFromConfig(bodyFromForm(values, 'custom').mcp_config));
    } else {
      try {
        const entries = parseEditorPayload(jsonText);
        if (entries.length !== 1) {
          message.error(t('mcp.bundleSwitchSingle'));
          return;
        }
        form.setFieldsValue({
          ...(entries[0].name ? { name: entries[0].name } : {}),
          ...formValuesFromConfig(entries[0].config),
        });
      } catch (error) {
        message.error(
          jsonErrorCode(error) === 'invalid_json'
            ? t('mcp.invalidJson')
            : jsonErrorCode(error) === 'empty_bundle'
              ? t('mcp.emptyBundle')
              : t('mcp.invalidConfig'),
        );
        return;
      }
    }
    setEditorMode(mode);
  };

  const closeEditor = () => {
    if (saving) return;
    setEditor(null);
  };

  const saveEditor = async () => {
    const creating = editor === 'create';
    const current = creating ? null : editor;
    const source = current?.source === 'builtin' ? 'builtin' : 'custom';
    let bodies: McpWriteBody[];
    if (editorMode === 'json') {
      let entries: ParsedServer[];
      try {
        entries = parseEditorPayload(jsonText);
      } catch (error) {
        const code = jsonErrorCode(error);
        message.error(
          code === 'invalid_json'
            ? t('mcp.invalidJson')
            : code === 'empty_bundle'
              ? t('mcp.emptyBundle')
              : t('mcp.invalidConfig'),
        );
        return;
      }
      if (!creating && entries.length !== 1) {
        message.error(t('mcp.bundleOnEdit'));
        return;
      }
      if (creating && entries.some((entry) => !entry.name?.trim())) {
        message.error(t('mcp.bundleNameRequired'));
        return;
      }
      const values = creating
        ? await form.validateFields(['enabled'])
        : await form.validateFields(['description', 'enabled']);
      const host = {
        description: creating ? '' : (values.description ?? '').trim(),
        enabled: values.enabled as boolean,
        enable_tools: current?.enable_tools ?? null,
        disable_tools: current?.disable_tools ?? null,
        execution_timeout: current?.execution_timeout ?? null,
      };
      bodies = entries.map((entry) =>
        writeBodyFromServer((entry.name || current?.name || '').trim(), entry.config, source, host),
      );
    } else {
      const values = await form.validateFields();
      const body = bodyFromForm(values, source);
      if (current) {
        body.enable_tools = current.enable_tools;
        body.disable_tools = current.disable_tools;
      }
      bodies = [body];
    }
    if (!creating && current && bodies[0].name !== current.name) {
      message.error(t('mcp.nameImmutable'));
      return;
    }
    setSaving(true);
    try {
      if (creating) {
        for (const body of bodies) {
          await mcpApi.create(body);
          setSourceFilter('custom');
        }
        message.success(
          bodies.length === 1
            ? t('mcp.created', { name: bodies[0].name })
            : t('mcp.createdMany', { count: bodies.length }),
        );
      } else {
        await mcpApi.update(bodies[0].name, bodies[0]);
        message.success(t('mcp.updated', { name: bodies[0].name }));
      }
      setEditor(null);
      await fetchItems();
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
      if (creating) {
        await fetchItems();
      }
    } finally {
      setSaving(false);
    }
  };

  const openTools = async (mcp: McpItem) => {
    setToolsMcp(mcp);
    setTools([]);
    setToolsError(null);
    setToolsLoading(true);
    try {
      const { tools: next } = await mcpApi.listTools(mcp.name);
      setTools(next);
      const unrestricted =
        mcp.enable_tools == null && (mcp.disable_tools == null || mcp.disable_tools.length === 0);
      setAllTools(unrestricted);
      setSelectedTools(next.filter((tool) => tool.enabled).map((tool) => tool.name));
    } catch (error) {
      setToolsError(errorMessage(error));
    } finally {
      setToolsLoading(false);
    }
  };

  const saveTools = async () => {
    if (!toolsMcp) return;
    setPendingAction(`tools:${toolsMcp.name}`);
    try {
      const listed = new Set(tools.map((tool) => tool.name));
      const selected = new Set(selectedTools);
      const disabled = allTools
        ? []
        : [
            ...(toolsMcp.disable_tools ?? []).filter(
              (name) => !listed.has(name) || !selected.has(name),
            ),
            ...tools.filter((tool) => !selected.has(tool.name)).map((tool) => tool.name),
          ];
      await mcpApi.setTools(
        toolsMcp.name,
        null,
        allTools || !disabled.length ? null : [...new Set(disabled)],
      );
      message.success(t('mcp.toolsSaved'));
      setToolsMcp(null);
      await fetchItems();
    } catch (error) {
      message.error(t('common.saveFailed', { message: errorMessage(error) }));
    } finally {
      setPendingAction(null);
    }
  };

  const visibleItems = sourceFilter === 'builtin' ? builtins : customs;
  const columns: TableColumnsType<McpItem> = [
    {
      title: t('mcp.name'),
      dataIndex: 'name',
      width: 250,
      render: (_, mcp) => (
        <div className={styles.toolIdentity}>
          <span className={styles.icon}>
            <CloudServerOutlined />
          </span>
          <div>
            <strong title={mcp.name}>{mcp.name}</strong>
            <Tag className={styles.tableTag} color={mcp.source === 'builtin' ? 'blue' : 'default'}>
              {mcp.source === 'builtin' ? t('mcp.builtin') : t('mcp.custom')}
            </Tag>
          </div>
        </div>
      ),
    },
    {
      title: t('mcp.descLabel'),
      dataIndex: 'description',
      render: (description: string) => (
        <span className={styles.descriptionCell}>{description || t('mcp.noDescription')}</span>
      ),
    },
    {
      title: t('mcp.type'),
      width: 100,
      render: (_, mcp) => (
        <Tag className={styles.tableTag}>
          {mcp.mcp_config.type === 'stdio_mcp' ? t('mcp.stdio') : t('mcp.http')}
        </Tag>
      ),
    },
    {
      title: t('mcp.endpoint'),
      width: 240,
      render: (_, mcp) => (
        <span className={styles.endpointCell} title={endpointLabel(mcp)}>
          {endpointLabel(mcp)}
        </span>
      ),
    },
    {
      title: t('mcp.status'),
      width: 140,
      render: (_, mcp) => {
        const actionKey = `mcp:${mcp.name}`;
        if (mcp.source === 'builtin') {
          return (
            <span className={`${styles.status} ${mcp.enabled ? styles.statusEnabled : ''}`}>
              <i />
              {mcp.enabled ? t('common.enabled') : t('common.disabled')}
            </span>
          );
        }
        return (
          <label className={styles.statusControl}>
            <Switch
              size="small"
              checked={mcp.enabled}
              loading={pendingAction === actionKey}
              onChange={(enabled) =>
                void runAction(
                  actionKey,
                  () => (enabled ? mcpApi.enable(mcp.name) : mcpApi.disable(mcp.name)),
                  enabled
                    ? t('mcp.enabledSuccess', { name: mcp.name })
                    : t('mcp.disabledSuccess', { name: mcp.name }),
                )
              }
            />
            <span>{mcp.enabled ? t('common.enabled') : t('common.disabled')}</span>
          </label>
        );
      },
    },
    {
      title: t('mcp.actions'),
      width: 220,
      fixed: 'right',
      render: (_, mcp) => {
        const isBuiltin = mcp.source === 'builtin';
        const actionKey = `mcp:${mcp.name}`;
        return (
          <div className={styles.tableActions}>
            <Button type="text" size="small" onClick={() => void openTools(mcp)}>
              {t('mcp.toolsTitle')}
            </Button>
            {!isBuiltin ? (
              <>
                <Button type="text" size="small" onClick={() => openEdit(mcp)}>
                  {t('common.edit')}
                </Button>
                <Popconfirm
                  title={t('mcp.deleteTitle')}
                  description={t('mcp.deleteDescription')}
                  okText={t('common.delete')}
                  cancelText={t('common.cancel')}
                  onConfirm={() =>
                    void runAction(
                      actionKey,
                      () => mcpApi.delete(mcp.name),
                      t('mcp.deleted', { name: mcp.name }),
                    )
                  }
                >
                  <Button danger type="text" size="small" loading={pendingAction === actionKey}>
                    {t('common.delete')}
                  </Button>
                </Popconfirm>
              </>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('mcp.title')} description={t('mcp.description')} />
      <main className={styles.main}>
        <div className={styles.toolbar}>
          <Select
            className={styles.sourceFilter}
            aria-label={t('mcp.sourceFilter')}
            value={sourceFilter}
            options={[
              { value: 'builtin', label: `${t('mcp.builtin')}（${builtins.length}）` },
              { value: 'custom', label: `${t('mcp.custom')}（${customs.length}）` },
            ]}
            onChange={(value) => setSourceFilter(value as 'builtin' | 'custom')}
          />
          <Button
            aria-label={t('common.refresh')}
            title={t('common.refresh')}
            icon={<ReloadOutlined spin={refreshing} />}
            disabled={loading || refreshing}
            aria-busy={refreshing}
            onClick={() => void refreshItems()}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('mcp.add')}
          </Button>
        </div>

        {loading ? (
          <div className={styles.state}>
            <Spin />
            <span>{t('mcp.loading')}</span>
          </div>
        ) : null}
        {!loading && loadError ? (
          <Alert
            showIcon
            type="error"
            message={t('common.refreshFailed', { message: loadError })}
            action={
              <Button size="small" loading={refreshing} onClick={() => void refreshItems()}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : null}
        {!loading && !loadError && !items.length ? <Empty description={t('mcp.empty')} /> : null}
        {!loading && !loadError ? (
          <section className={styles.collection} aria-label={t('mcp.title')}>
            <Table<McpItem>
              className={styles.table}
              rowKey="name"
              columns={columns}
              dataSource={visibleItems}
              pagination={false}
              scroll={{ x: 1050 }}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcp.empty')} />
                ),
              }}
            />
          </section>
        ) : null}
      </main>

      <RouteDrawer
        open={editor !== null}
        title={editor === 'create' ? t('mcp.add') : t('mcp.edit')}
        width={560}
        destroyOnClose
        onClose={closeEditor}
        extra={
          <Button type="primary" loading={saving} onClick={() => void saveEditor()}>
            {t('common.save')}
          </Button>
        }
      >
        <div className={styles.modeSwitch}>
          <Segmented
            value={editorMode}
            options={[
              { value: 'json', label: t('mcp.modeJson') },
              { value: 'form', label: t('mcp.modeForm') },
            ]}
            onChange={(value) => switchEditorMode(value as 'form' | 'json')}
          />
        </div>
        {editor !== 'create' ? (
          <Alert
            showIcon
            type="warning"
            message={t('mcp.secretRewriteHint')}
            style={{ marginBottom: 16 }}
          />
        ) : null}
        <Form form={form} layout="vertical" initialValues={emptyForm()}>
          {editor === 'create' && editorMode === 'json' ? null : (
            <>
              <Form.Item
                label={t('mcp.name')}
                name="name"
                rules={[{ required: true, message: t('mcp.nameRequired') }]}
              >
                <Input disabled={editor !== 'create'} placeholder={t('mcp.namePlaceholder')} />
              </Form.Item>
              <Form.Item label={t('mcp.descLabel')} name="description">
                <Input placeholder={t('mcp.descPlaceholder')} />
              </Form.Item>
            </>
          )}
          <Form.Item label={t('mcp.enabled')} name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          {editorMode === 'json' ? (
            <Form.Item label={t('mcp.configJson')} required>
              <Input.TextArea
                className={styles.jsonEditor}
                rows={16}
                spellCheck={false}
                value={jsonText}
                placeholder={t('mcp.configJsonPlaceholder')}
                onChange={(event) => setJsonText(event.target.value)}
              />
            </Form.Item>
          ) : (
            <>
              <Form.Item label={t('mcp.type')} name="type">
                <Select
                  options={[
                    { value: 'http_mcp', label: t('mcp.http') },
                    { value: 'stdio_mcp', label: t('mcp.stdio') },
                  ]}
                  onChange={(value) => {
                    form.setFieldValue('is_stateful', value === 'stdio_mcp');
                  }}
                />
              </Form.Item>
              {transport === 'stdio_mcp' ? (
                <>
                  <Form.Item
                    label={t('mcp.command')}
                    name="command"
                    rules={[{ required: true, message: t('mcp.commandRequired') }]}
                  >
                    <Input placeholder={t('mcp.commandPlaceholder')} />
                  </Form.Item>
                  <Form.Item label={t('mcp.args')} name="argsText">
                    <Input.TextArea rows={4} spellCheck={false} />
                  </Form.Item>
                  <Form.Item label={t('mcp.cwd')} name="cwd">
                    <Input />
                  </Form.Item>
                  <Form.Item label={t('mcp.stateful')} name="is_stateful" valuePropName="checked">
                    <Switch disabled />
                  </Form.Item>
                  <p className={styles.fieldHint}>{t('mcp.stdioStatefulHint')}</p>
                  <Form.List name="env">
                    {(fields, { add, remove }) => (
                      <div className={styles.pairList}>
                        <span>{t('mcp.env')}</span>
                        {fields.map((field) => (
                          <div className={styles.pairRow} key={field.key}>
                            <Form.Item name={[field.name, 'key']} noStyle>
                              <Input placeholder={t('mcp.headerName')} />
                            </Form.Item>
                            <Form.Item name={[field.name, 'value']} noStyle>
                              <Input.Password placeholder={t('mcp.headerValue')} />
                            </Form.Item>
                            <Button type="text" danger onClick={() => remove(field.name)}>
                              {t('common.delete')}
                            </Button>
                          </div>
                        ))}
                        <Button
                          type="dashed"
                          icon={<PlusOutlined />}
                          onClick={() => add({ key: '', value: '' })}
                        >
                          {t('mcp.addPair')}
                        </Button>
                      </div>
                    )}
                  </Form.List>
                </>
              ) : (
                <>
                  <Form.Item
                    label={t('mcp.url')}
                    name="url"
                    rules={[{ required: true, message: t('mcp.urlRequired') }]}
                  >
                    <Input placeholder={t('mcp.urlPlaceholder')} />
                  </Form.Item>
                  <Form.Item label={t('mcp.timeout')} name="timeout">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item label={t('mcp.stateful')} name="is_stateful" valuePropName="checked">
                    <Switch />
                  </Form.Item>
                  <Form.List name="headers">
                    {(fields, { add, remove }) => (
                      <div className={styles.pairList}>
                        <span>{t('mcp.headers')}</span>
                        {fields.map((field) => (
                          <div className={styles.pairRow} key={field.key}>
                            <Form.Item name={[field.name, 'key']} noStyle>
                              <Input placeholder={t('mcp.headerName')} />
                            </Form.Item>
                            <Form.Item name={[field.name, 'value']} noStyle>
                              <Input.Password placeholder={t('mcp.headerValue')} />
                            </Form.Item>
                            <Button type="text" danger onClick={() => remove(field.name)}>
                              {t('common.delete')}
                            </Button>
                          </div>
                        ))}
                        <Button
                          type="dashed"
                          icon={<PlusOutlined />}
                          onClick={() => add({ key: '', value: '' })}
                        >
                          {t('mcp.addPair')}
                        </Button>
                      </div>
                    )}
                  </Form.List>
                </>
              )}
              <Form.Item label={t('mcp.executionTimeout')} name="execution_timeout">
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </>
          )}
        </Form>
      </RouteDrawer>

      <RouteDrawer
        open={toolsMcp !== null}
        title={t('mcp.toolsTitle')}
        width={420}
        onClose={() => setToolsMcp(null)}
        extra={
          toolsMcp ? (
            <Button
              type="primary"
              loading={pendingAction === `tools:${toolsMcp.name}`}
              onClick={() => void saveTools()}
            >
              {t('common.save')}
            </Button>
          ) : null
        }
      >
        {toolsLoading ? (
          <div className={styles.state}>
            <Spin />
            <span>{t('mcp.toolsLoading')}</span>
          </div>
        ) : null}
        {toolsError ? <Alert showIcon type="error" message={toolsError} /> : null}
        {!toolsLoading && !toolsError && !tools.length ? (
          <Empty description={t('mcp.toolsEmpty')} />
        ) : null}
        {!toolsLoading && !toolsError && tools.length ? (
          <div className={styles.toolList}>
            <label className={styles.toolRow}>
              <Switch
                size="small"
                checked={allTools}
                onChange={(checked) => {
                  setAllTools(checked);
                  if (checked) setSelectedTools(tools.map((tool) => tool.name));
                }}
              />
              <span>{t('mcp.allTools')}</span>
            </label>
            {tools.map((tool) => (
              <label className={styles.toolRow} key={tool.name}>
                <Switch
                  size="small"
                  disabled={allTools}
                  checked={allTools || selectedTools.includes(tool.name)}
                  onChange={(checked) => {
                    setSelectedTools((current) =>
                      checked
                        ? [...current, tool.name]
                        : current.filter((name) => name !== tool.name),
                    );
                  }}
                />
                <div>
                  <strong>{tool.name}</strong>
                  <p>{tool.description || t('mcp.noDescription')}</p>
                </div>
              </label>
            ))}
          </div>
        ) : null}
      </RouteDrawer>
    </div>
  );
}
