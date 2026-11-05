import {
  ApiOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import {
  Alert,
  Checkbox,
  Empty,
  Modal,
  Popconfirm,
  Segmented,
  Spin,
  Switch,
  Tag,
  message,
} from 'antd';
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import {
  mcpHubApi,
  type McpClient,
  type McpClientWrite,
  type McpConfigType,
  type McpTool,
} from '@/features/console/api/mcp';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import { buildNewConsoleSessionPath } from '@/features/console/model/sessionNavigation';
import styles from './index.module.less';

type EditorMode = 'form' | 'json';

interface EditorState {
  name: string;
  display_name: string;
  description: string;
  enabled: boolean;
  transport: McpConfigType;
  command: string;
  argsText: string;
  envText: string;
  cwd: string;
  url: string;
  headersText: string;
}

const EMPTY_EDITOR: EditorState = {
  name: '',
  display_name: '',
  description: '',
  enabled: true,
  transport: 'stdio_mcp',
  command: '',
  argsText: '',
  envText: '{}',
  cwd: '',
  url: '',
  headersText: '{}',
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function transportLabel(type: McpConfigType): string {
  return type === 'stdio_mcp' ? 'stdio' : 'HTTP';
}

function clientToEditor(client: McpClient): EditorState {
  const config = client.mcp_config;
  return {
    name: client.name,
    display_name: client.display_name,
    description: client.description,
    enabled: client.enabled,
    transport: config.type,
    command: config.command || '',
    argsText: (config.args ?? []).join('\n'),
    envText: JSON.stringify(config.env ?? {}, null, 2),
    cwd: config.cwd || '',
    url: config.url || '',
    headersText: JSON.stringify(config.headers ?? {}, null, 2),
  };
}

function parseObject(text: string, label: string): Record<string, string> {
  const trimmed = text.trim() || '{}';
  const parsed = JSON.parse(trimmed) as unknown;
  if (
    parsed === null
    || typeof parsed !== 'object'
    || Array.isArray(parsed)
    || Object.values(parsed).some((value) => typeof value !== 'string')
  ) {
    throw new Error(`${label} 必须是字符串键值对象`);
  }
  return parsed as Record<string, string>;
}

function editorToWrite(state: EditorState): McpClientWrite {
  const stdio = state.transport === 'stdio_mcp';
  return {
    name: state.name.trim(),
    display_name: state.display_name.trim(),
    description: state.description.trim(),
    enabled: state.enabled,
    is_stateful: stdio,
    mcp_config: stdio
      ? {
        type: 'stdio_mcp',
        command: state.command.trim(),
        args: state.argsText.split('\n').map((item) => item.trim()).filter(Boolean),
        env: parseObject(state.envText, 'env'),
        cwd: state.cwd.trim() || null,
      }
      : {
        type: 'http_mcp',
        url: state.url.trim(),
        headers: parseObject(state.headersText, 'headers'),
      },
  };
}

export default function McpHubPage() {
  const navigate = useNavigate();
  const [clients, setClients] = useState<McpClient[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<EditorMode>('json');
  const [editing, setEditing] = useState<McpClient | null>(null);
  const [editor, setEditor] = useState<EditorState>(EMPTY_EDITOR);
  const [jsonText, setJsonText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [toolsClient, setToolsClient] = useState<McpClient | null>(null);
  const [tools, setTools] = useState<McpTool[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [selectedTools, setSelectedTools] = useState<string[] | null>(null);

  const fetchClients = useCallback(async () => {
    const response = await mcpHubApi.list();
    setClients(response.clients);
    setLoadError(null);
  }, []);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'MCP Hub · QwenPaw Data';
    void fetchClients()
      .catch((error) => setLoadError(errorMessage(error)))
      .finally(() => setLoading(false));
    return () => {
      document.title = previousTitle;
    };
  }, [fetchClients]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await fetchClients();
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setRefreshing(false);
    }
  }, [fetchClients]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleClients = useMemo(
    () => clients.filter((client) => {
      if (!normalizedQuery) return true;
      return [client.name, client.display_name, client.description, client.mcp_config.type]
        .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
    }),
    [clients, normalizedQuery],
  );

  const runAction = useCallback(async (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => {
    setPendingAction(key);
    try {
      await action();
      await fetchClients();
      message.success(success);
    } catch (error) {
      message.error(`操作失败：${errorMessage(error)}`);
    } finally {
      setPendingAction(null);
    }
  }, [fetchClients]);

  const openCreate = () => {
    setEditing(null);
    setEditor(EMPTY_EDITOR);
    setEditorMode('json');
    setJsonText('');
    setEditorOpen(true);
  };

  const openEdit = (client: McpClient) => {
    setEditing(client);
    setEditor(clientToEditor(client));
    setEditorMode('form');
    setJsonText('');
    setEditorOpen(true);
  };

  const submitEditor = async () => {
    setSubmitting(true);
    try {
      if (!editing && editorMode === 'json') {
        const parsed = JSON.parse(jsonText) as unknown;
        const { clients: imported } = await mcpHubApi.importConfig(parsed);
        await fetchClients();
        setEditorOpen(false);
        message.success(`已导入 ${imported.length} 个 MCP`);
        return;
      }
      const body = editorToWrite(editor);
      if (editing) {
        await mcpHubApi.update(editing.name, {
          ...body,
          name: editing.name,
          enable_tools: editing.enable_tools,
        });
        message.success(`${editing.name} 已更新`);
      } else {
        await mcpHubApi.create(body);
        message.success(`${body.name} 已创建`);
      }
      await fetchClients();
      setEditorOpen(false);
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSubmitting(false);
    }
  };

  const openTools = async (client: McpClient) => {
    setToolsClient(client);
    setTools([]);
    setSelectedTools(client.enable_tools);
    setToolsLoading(true);
    try {
      const response = await mcpHubApi.listTools(client.name);
      setTools(response.tools);
      setSelectedTools(
        client.enable_tools === null
          ? response.tools.map((tool) => tool.name)
          : client.enable_tools,
      );
    } catch (error) {
      message.error(`工具列表加载失败：${errorMessage(error)}`);
    } finally {
      setToolsLoading(false);
    }
  };

  const saveTools = async () => {
    if (!toolsClient || toolsClient.builtin) return;
    const allSelected = tools.length > 0
      && selectedTools !== null
      && selectedTools.length === tools.length;
    setSubmitting(true);
    try {
      await mcpHubApi.setTools(toolsClient.name, allSelected ? null : selectedTools);
      await fetchClients();
      setToolsClient(null);
      message.success(`${toolsClient.name} 工具白名单已更新`);
    } catch (error) {
      message.error(`保存失败：${errorMessage(error)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader
        parentLabel="工作空间"
        title="MCP Hub"
        onParentClick={() => navigate(buildNewConsoleSessionPath())}
      />

      <main className={styles.main}>
        <section className={styles.toolbar} aria-label="MCP 筛选与操作">
          <span className={styles.toolbarDescription}>
            为默认 Agent 添加本地 stdio 或远程 HTTP MCP，配置立即写入工作区。
          </span>
          <Input.Search
            allowClear
            className={styles.search}
            placeholder="搜索 MCP 名称或描述"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Button
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={() => void refresh()}
          >
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            添加 MCP
          </Button>
        </section>

        {loading ? (
          <div className={styles.state}><Spin /><span>正在加载 MCP…</span></div>
        ) : null}
        {!loading && loadError ? (
          <Alert
            showIcon
            type="error"
            message="MCP 加载失败"
            description={loadError}
            action={<Button size="small" onClick={() => void refresh()}>重试</Button>}
          />
        ) : null}

        {!loading && !loadError && !visibleClients.length ? (
          <div className={styles.empty}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={normalizedQuery ? '没有匹配的 MCP' : '还没有配置 MCP'}
            />
          </div>
        ) : null}

        {!loading && !loadError && visibleClients.length ? (
          <section className={styles.skillCollection} aria-label="MCP 客户端">
            <div className={styles.skillGrid}>
              {visibleClients.map((client) => {
                const actionKey = client.name;
                const actionLoading = pendingAction === actionKey;
                return (
                  <article className={styles.skillCard} key={client.name}>
                    <div className={styles.cardTopRow}>
                      <span className={styles.skillIcon}><ApiOutlined /></span>
                      <div className={styles.titleRow}>
                        <h3 title={client.display_name || client.name}>
                          {client.display_name || client.name}
                        </h3>
                        <Tag color={client.builtin ? 'blue' : 'default'}>
                          {client.builtin ? '内置' : transportLabel(client.mcp_config.type)}
                        </Tag>
                      </div>
                      <span className={`${styles.status} ${client.enabled ? styles.statusEnabled : ''}`}>
                        <i />{client.enabled ? '已启用' : '已停用'}
                      </span>
                    </div>
                    <p className={styles.description}>
                      {client.description
                        || (client.mcp_config.type === 'stdio_mcp'
                          ? client.mcp_config.command
                          : client.mcp_config.url)
                        || '暂无描述'}
                    </p>
                    <div className={styles.metaRow}>
                      <span>标识</span>
                      <strong>{client.name}</strong>
                    </div>
                    <footer className={styles.cardFooter}>
                      <label>
                        <Switch
                          size="small"
                          checked={client.enabled}
                          disabled={client.builtin}
                          loading={actionLoading}
                          onChange={(enabled) => void runAction(
                            actionKey,
                            () => mcpHubApi.setEnabled(client.name, enabled),
                            enabled ? `${client.name} 已启用` : `${client.name} 已停用`,
                          )}
                        />
                        <span>{client.builtin ? '系统内置' : client.enabled ? '运行中' : '禁用中'}</span>
                      </label>
                      <div>
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void openTools(client)}
                        >
                          工具
                        </Button>
                        {!client.builtin ? (
                          <>
                            <Button
                              type="text"
                              size="small"
                              icon={<EditOutlined />}
                              onClick={() => openEdit(client)}
                            >
                              编辑
                            </Button>
                            <Popconfirm
                              title="删除这个 MCP？"
                              description="会从默认 Agent 移除，并同步到工作区。"
                              okText="删除"
                              cancelText="取消"
                              onConfirm={() => runAction(
                                actionKey,
                                () => mcpHubApi.remove(client.name),
                                `${client.name} 已删除`,
                              )}
                            >
                              <Button
                                danger
                                type="text"
                                size="small"
                                icon={<DeleteOutlined />}
                                loading={actionLoading}
                              >
                                删除
                              </Button>
                            </Popconfirm>
                          </>
                        ) : null}
                      </div>
                    </footer>
                  </article>
                );
              })}
            </div>
          </section>
        ) : null}
      </main>

      <Modal
        title={editing ? `编辑 ${editing.name}` : '添加 MCP'}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={() => void submitEditor()}
        confirmLoading={submitting}
        okText={editing ? '保存' : '创建'}
        destroyOnHidden
        width={640}
      >
        {!editing ? (
          <Segmented<EditorMode>
            value={editorMode}
            options={[
              { value: 'json', label: 'JSON 导入' },
              { value: 'form', label: '表单' },
            ]}
            onChange={setEditorMode}
            style={{ marginBottom: 16 }}
          />
        ) : null}

        {editorMode === 'json' && !editing ? (
          <Input.TextArea
            className={styles.jsonInput}
            rows={16}
            value={jsonText}
            onChange={(event) => setJsonText(event.target.value)}
            placeholder={'{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]\n    }\n  }\n}'}
          />
        ) : (
          <div className={styles.form}>
            <div className={styles.formRow}>
              <label htmlFor="mcp-name">标识</label>
              <Input
                id="mcp-name"
                value={editor.name}
                disabled={Boolean(editing)}
                onChange={(event) => setEditor((current) => ({ ...current, name: event.target.value }))}
                placeholder="filesystem"
              />
            </div>
            <div className={styles.formRow}>
              <label htmlFor="mcp-display-name">显示名</label>
              <Input
                id="mcp-display-name"
                value={editor.display_name}
                onChange={(event) => setEditor((current) => ({
                  ...current,
                  display_name: event.target.value,
                }))}
              />
            </div>
            <div className={styles.formRow}>
              <label htmlFor="mcp-description">描述</label>
              <Input
                id="mcp-description"
                value={editor.description}
                onChange={(event) => setEditor((current) => ({
                  ...current,
                  description: event.target.value,
                }))}
              />
            </div>
            <div className={styles.formRow}>
              <label>传输</label>
              <Segmented<McpConfigType>
                value={editor.transport}
                options={[
                  { value: 'stdio_mcp', label: 'stdio' },
                  { value: 'http_mcp', label: 'HTTP' },
                ]}
                onChange={(transport) => setEditor((current) => ({ ...current, transport }))}
              />
            </div>
            {editor.transport === 'stdio_mcp' ? (
              <>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-command">命令</label>
                  <Input
                    id="mcp-command"
                    value={editor.command}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      command: event.target.value,
                    }))}
                    placeholder="npx"
                  />
                </div>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-args">参数（每行一个）</label>
                  <Input.TextArea
                    id="mcp-args"
                    rows={4}
                    value={editor.argsText}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      argsText: event.target.value,
                    }))}
                  />
                </div>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-cwd">工作目录</label>
                  <Input
                    id="mcp-cwd"
                    value={editor.cwd}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      cwd: event.target.value,
                    }))}
                  />
                </div>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-env">环境变量 JSON</label>
                  <Input.TextArea
                    id="mcp-env"
                    className={styles.jsonInput}
                    rows={4}
                    value={editor.envText}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      envText: event.target.value,
                    }))}
                  />
                </div>
              </>
            ) : (
              <>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-url">URL</label>
                  <Input
                    id="mcp-url"
                    value={editor.url}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      url: event.target.value,
                    }))}
                    placeholder="https://example.com/mcp"
                  />
                </div>
                <div className={styles.formRow}>
                  <label htmlFor="mcp-headers">请求头 JSON</label>
                  <Input.TextArea
                    id="mcp-headers"
                    className={styles.jsonInput}
                    rows={4}
                    value={editor.headersText}
                    onChange={(event) => setEditor((current) => ({
                      ...current,
                      headersText: event.target.value,
                    }))}
                  />
                </div>
              </>
            )}
          </div>
        )}
      </Modal>

      <Modal
        title={toolsClient ? `${toolsClient.display_name || toolsClient.name} 工具` : '工具'}
        open={Boolean(toolsClient)}
        onCancel={() => setToolsClient(null)}
        onOk={() => void saveTools()}
        okButtonProps={{ disabled: Boolean(toolsClient?.builtin) || toolsLoading }}
        confirmLoading={submitting}
        okText="保存白名单"
        destroyOnHidden
        width={560}
      >
        {toolsLoading ? (
          <div className={styles.state}><Spin /><span>正在查询工具…</span></div>
        ) : null}
        {!toolsLoading && !tools.length ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有查询到工具" />
        ) : null}
        {!toolsLoading && tools.length ? (
          <div className={styles.toolList}>
            {tools.map((tool) => (
              <label className={styles.toolItem} key={tool.name}>
                <Checkbox
                  checked={selectedTools?.includes(tool.name) ?? false}
                  disabled={Boolean(toolsClient?.builtin)}
                  onChange={(event) => {
                    setSelectedTools((current) => {
                      const selected = new Set(current ?? []);
                      if (event.target.checked) selected.add(tool.name);
                      else selected.delete(tool.name);
                      return Array.from(selected);
                    });
                  }}
                />
                <span>
                  <strong>{tool.name}</strong>
                  <p>{tool.description || '暂无描述'}</p>
                </span>
              </label>
            ))}
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
