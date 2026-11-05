import {
  CloudUploadOutlined,
  DeleteOutlined,
  DownOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import {
  Alert,
  Empty,
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
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import {
  skillHubApi,
  type AgentSkill,
  type PoolSkill,
} from '@/features/console/api/skills';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import { buildNewConsoleSessionPath } from '@/features/console/model/sessionNavigation';
import styles from './index.module.less';

type SkillView = 'agent' | 'pool';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function matchesSearch(skill: PoolSkill | AgentSkill, query: string): boolean {
  if (!query) return true;
  return [skill.name, skill.description, skill.source]
    .some((value) => value.toLocaleLowerCase().includes(query));
}

function sourceLabel(source: string): string {
  return source === 'builtin' ? '内置' : '自定义';
}

export default function SkillsHubPage() {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [view, setView] = useState<SkillView>('agent');
  const [query, setQuery] = useState('');
  const [poolSkills, setPoolSkills] = useState<PoolSkill[]>([]);
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [builtinExpanded, setBuiltinExpanded] = useState(false);
  const [customExpanded, setCustomExpanded] = useState(true);

  const fetchSkills = useCallback(async () => {
    const [poolResponse, agentResponse] = await Promise.all([
      skillHubApi.listPool(),
      skillHubApi.listAgent(),
    ]);
    setPoolSkills(poolResponse.skills);
    setAgentSkills(agentResponse.skills);
    setLoadError(null);
  }, []);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'Skills Hub · QwenPaw Data';
    void fetchSkills()
      .catch((error) => setLoadError(errorMessage(error)))
      .finally(() => setLoading(false));
    return () => {
      document.title = previousTitle;
    };
  }, [fetchSkills]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await fetchSkills();
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setRefreshing(false);
    }
  }, [fetchSkills]);

  const installedNames = useMemo(
    () => new Set(agentSkills.map((skill) => skill.name)),
    [agentSkills],
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleAgentSkills = useMemo(
    () => agentSkills.filter((skill) => matchesSearch(skill, normalizedQuery)),
    [agentSkills, normalizedQuery],
  );
  const visiblePoolSkills = useMemo(
    () => poolSkills.filter((skill) => matchesSearch(skill, normalizedQuery)),
    [normalizedQuery, poolSkills],
  );
  const visibleAgentBuiltinSkills = useMemo(
    () => visibleAgentSkills.filter((skill) => skill.source === 'builtin'),
    [visibleAgentSkills],
  );
  const visibleAgentCustomSkills = useMemo(
    () => visibleAgentSkills.filter((skill) => skill.source !== 'builtin'),
    [visibleAgentSkills],
  );
  const visiblePoolBuiltinSkills = useMemo(
    () => visiblePoolSkills.filter((skill) => skill.source === 'builtin'),
    [visiblePoolSkills],
  );
  const visiblePoolCustomSkills = useMemo(
    () => visiblePoolSkills.filter((skill) => skill.source !== 'builtin'),
    [visiblePoolSkills],
  );
  const runAction = useCallback(async (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => {
    setPendingAction(key);
    try {
      await action();
      await fetchSkills();
      message.success(success);
    } catch (error) {
      message.error(`操作失败：${errorMessage(error)}`);
    } finally {
      setPendingAction(null);
    }
  }, [fetchSkills]);

  const importZip = useCallback(async (file: File) => {
    if (!file.name.toLocaleLowerCase().endsWith('.zip')) {
      message.warning('请选择 ZIP 格式的 Skill 包');
      return;
    }
    setUploading(true);
    try {
      const { imported } = await skillHubApi.importPool(file);
      await fetchSkills();
      message.success(`已导入 ${imported.length} 个 Skill`);
    } catch (error) {
      message.error(`导入失败：${errorMessage(error)}`);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }, [fetchSkills]);

  const visibleSkills = view === 'agent' ? visibleAgentSkills : visiblePoolSkills;
  const visibleBuiltinCount = view === 'agent'
    ? visibleAgentBuiltinSkills.length
    : visiblePoolBuiltinSkills.length;
  const visibleCustomCount = view === 'agent'
    ? visibleAgentCustomSkills.length
    : visiblePoolCustomSkills.length;
  const showBuiltinSkills = builtinExpanded || Boolean(normalizedQuery);
  const showCustomSkills = customExpanded || Boolean(normalizedQuery);

  const renderAgentCards = (skills: AgentSkill[]) => skills.map((skill) => {
    const actionKey = `agent:${skill.name}`;
    const actionLoading = pendingAction === actionKey;
    const isBuiltin = skill.source === 'builtin';
    return (
      <article className={styles.skillCard} key={skill.name}>
        <div className={styles.cardTopRow}>
          <span className={styles.skillIcon}><ThunderboltOutlined /></span>
          <div className={styles.titleRow}>
            <h3 title={skill.name}>{skill.name}</h3>
            <Tag color={isBuiltin ? 'blue' : 'default'}>
              {sourceLabel(skill.source)}
            </Tag>
          </div>
          <span className={`${styles.status} ${skill.enabled ? styles.statusEnabled : ''}`}>
            <i />{skill.enabled ? '已启用' : '已停用'}
          </span>
        </div>
        <p className={styles.description}>{skill.description || '暂无描述'}</p>
        <div className={styles.metaRow}>
          <span>安装来源</span>
          <strong>{skill.installed_from || skill.source}</strong>
        </div>
        <footer className={styles.cardFooter}>
          <label>
            <Switch
              size="small"
              checked={skill.enabled}
              loading={actionLoading}
              onChange={(enabled) => void runAction(
                actionKey,
                () => skillHubApi.setAgentEnabled(skill.name, enabled),
                enabled ? `${skill.name} 已启用` : `${skill.name} 已停用`,
              )}
            />
            <span>{skill.enabled ? '运行中' : '禁用中'}</span>
          </label>
          {!isBuiltin ? (
            <Popconfirm
              title="卸载这个 Skill？"
              description="只删除 Agent 副本，Skill Pool 中的来源不受影响。"
              okText="卸载"
              cancelText="取消"
              onConfirm={() => runAction(
                actionKey,
                () => skillHubApi.uninstallAgent(skill.name),
                `${skill.name} 已从 Agent 卸载`,
              )}
            >
              <Button
                danger
                type="text"
                size="small"
                icon={<DeleteOutlined />}
                loading={actionLoading}
              >
                卸载
              </Button>
            </Popconfirm>
          ) : null}
        </footer>
      </article>
    );
  });

  const renderPoolCards = (skills: PoolSkill[]) => skills.map((skill) => {
    const installed = installedNames.has(skill.name);
    const actionKey = `pool:${skill.name}`;
    const actionLoading = pendingAction === actionKey;
    const deletable = !skill.protected && skill.source !== 'builtin';
    return (
      <article className={styles.skillCard} key={skill.name}>
        <div className={styles.cardTopRow}>
          <span className={styles.skillIcon}><ThunderboltOutlined /></span>
          <div className={styles.titleRow}>
            <h3 title={skill.name}>{skill.name}</h3>
            <Tag color={skill.source === 'builtin' ? 'blue' : 'default'}>
              {sourceLabel(skill.source)}
            </Tag>
          </div>
          <span className={`${styles.status} ${installed ? styles.statusEnabled : ''}`}>
            <i />{installed ? '已安装' : '未安装'}
          </span>
        </div>
        <p className={styles.description}>{skill.description || '暂无描述'}</p>
        <div className={styles.metaRow}>
          <span>分组</span>
          <strong>{skill.group || 'custom'}</strong>
        </div>
        <footer className={styles.cardFooter}>
          <span className={styles.poolHint}>
            {skill.protected ? '系统保护' : '用户导入'}
          </span>
          <div>
            {deletable ? (
              <Popconfirm
                title="从 Skill Pool 删除？"
                description="已安装到 Agent 的副本不会被同时删除。"
                okText="删除"
                cancelText="取消"
                onConfirm={() => runAction(
                  actionKey,
                  () => skillHubApi.deletePool(skill.name),
                  `${skill.name} 已从 Skill Pool 删除`,
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
            ) : null}
            <Button
              type={installed ? 'default' : 'primary'}
              size="small"
              icon={<PlusOutlined />}
              disabled={installed}
              loading={actionLoading}
              onClick={() => void runAction(
                actionKey,
                () => skillHubApi.installAgent(skill.name),
                `${skill.name} 已安装到 Agent`,
              )}
            >
              {installed ? '已安装' : '安装到 Agent'}
            </Button>
          </div>
        </footer>
      </article>
    );
  });

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader
        parentLabel="工作空间"
        title="Skills Hub"
        onParentClick={() => navigate(buildNewConsoleSessionPath())}
      />

      <main className={styles.main}>
        <section className={styles.toolbar} aria-label="Skill 筛选与操作">
          <Segmented<SkillView>
            value={view}
            options={[
              { value: 'agent', label: `Agent Skills · ${agentSkills.length}` },
              { value: 'pool', label: `Skill Pool · ${poolSkills.length}` },
            ]}
            onChange={(value) => {
              setView(value);
              setBuiltinExpanded(false);
              setCustomExpanded(true);
            }}
          />
          <span className={styles.toolbarDescription}>
            从 Skill Pool 安装能力到默认 Agent，并按需启用或停用。
          </span>
          <Input.Search
            allowClear
            className={styles.search}
            placeholder="搜索 Skill 名称或描述"
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
          {view === 'pool' ? (
            <>
              <input
                ref={fileInputRef}
                className={styles.fileInput}
                type="file"
                accept=".zip,application/zip"
                onChange={(event) => {
                  const [file] = Array.from(event.target.files ?? []);
                  if (file) void importZip(file);
                }}
              />
              <Button
                type="primary"
                icon={<CloudUploadOutlined />}
                loading={uploading}
                onClick={() => fileInputRef.current?.click()}
              >
                导入 ZIP
              </Button>
            </>
          ) : null}
        </section>

        {loading ? (
          <div className={styles.state}><Spin /><span>正在加载 Skills…</span></div>
        ) : null}
        {!loading && loadError ? (
          <Alert
            showIcon
            type="error"
            message="Skills 加载失败"
            description={loadError}
            action={<Button size="small" onClick={() => void refresh()}>重试</Button>}
          />
        ) : null}

        {!loading && !loadError && !visibleSkills.length ? (
          <div className={styles.empty}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={normalizedQuery
                ? '没有匹配的 Skill'
                : view === 'agent'
                  ? '默认 Agent 尚未安装 Skill'
                  : 'Skill Pool 为空'}
            />
          </div>
        ) : null}

        {!loading && !loadError && visibleSkills.length ? (
          <section className={styles.skillCollection} aria-label={view === 'agent' ? 'Agent Skills' : 'Skill Pool'}>
            {visibleBuiltinCount ? (
              <div className={styles.skillGroup}>
                <button
                  className={styles.groupToggle}
                  type="button"
                  aria-expanded={showBuiltinSkills}
                  onClick={() => setBuiltinExpanded((expanded) => !expanded)}
                >
                  <div>
                    <strong>
                      内置 Skill<span className={styles.groupCount}>（{visibleBuiltinCount}）</span>
                    </strong>
                    <span>系统预置能力</span>
                  </div>
                  <span className={styles.groupToggleMeta}>
                    <span>{showBuiltinSkills ? '收起' : '展开'}</span>
                    <DownOutlined className={showBuiltinSkills ? styles.groupToggleExpanded : ''} />
                  </span>
                </button>
                {showBuiltinSkills ? (
                  <div className={styles.skillGrid}>
                    {view === 'agent'
                      ? renderAgentCards(visibleAgentBuiltinSkills)
                      : renderPoolCards(visiblePoolBuiltinSkills)}
                  </div>
                ) : null}
              </div>
            ) : null}

            {visibleCustomCount ? (
              <div className={styles.skillGroup}>
                <button
                  className={styles.groupToggle}
                  type="button"
                  aria-expanded={showCustomSkills}
                  onClick={() => setCustomExpanded((expanded) => !expanded)}
                >
                  <div>
                    <strong>
                      自定义 Skill<span className={styles.groupCount}>（{visibleCustomCount}）</span>
                    </strong>
                    <span>用户导入与安装的能力</span>
                  </div>
                  <span className={styles.groupToggleMeta}>
                    <span>{showCustomSkills ? '收起' : '展开'}</span>
                    <DownOutlined className={showCustomSkills ? styles.groupToggleExpanded : ''} />
                  </span>
                </button>
                {showCustomSkills ? (
                  <div className={styles.skillGrid}>
                    {view === 'agent'
                      ? renderAgentCards(visibleAgentCustomSkills)
                      : renderPoolCards(visiblePoolCustomSkills)}
                  </div>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}
      </main>
    </div>
  );
}
