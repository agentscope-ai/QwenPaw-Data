import {
  CloudUploadOutlined,
  CopyOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Input } from '@agentscope-ai/design';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import {
  Alert,
  Empty,
  Popconfirm,
  Segmented,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
  type TableColumnsType,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  skillHubApi,
  type AgentSkill,
  type AgentSkillDetail,
  type SkillRevision,
  type SkillRevisionDetail,
} from '@/features/console/api/skills';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader/index';
import RouteDrawer from '@/features/console/components/RouteDrawer';
import { useAppI18n } from '@/i18n';
import type { MessageKey } from '@/i18n/locales';
import styles from './index.module.less';

type SkillSourceFilter = 'builtin' | 'custom';
type SkillPreviewMode = 'rendered' | 'source';
type SkillEditorMode = 'create' | 'edit';

interface SkillEditorTarget {
  mode: SkillEditorMode;
  name?: string;
}

const EMPTY_SKILL_DOCUMENT = '---\nname: \ndescription: \n---\n\n';

const ORIGIN_LABEL_KEYS: Record<string, MessageKey> = {
  create: 'skills.originCreate',
  install: 'skills.originInstall',
  import: 'skills.originImport',
  manual_edit: 'skills.originManualEdit',
  evolution_accept: 'skills.originEvolution',
  revert: 'skills.originRevert',
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function matchesSearch(skill: AgentSkill, query: string): boolean {
  if (!query) return true;
  return [skill.name, skill.description, skill.group].some((value) =>
    value.toLocaleLowerCase().includes(query),
  );
}

function stripSkillFrontmatter(content: string): string {
  const lines = content.replace(/\r\n?/g, '\n').split('\n');
  if (lines[0]?.trim() !== '---') return content;
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  return end < 0
    ? content
    : lines
        .slice(end + 1)
        .join('\n')
        .trimStart();
}

function replaceFrontmatterName(content: string, name: string): string | null {
  const lines = content.replace(/\r\n?/g, '\n').split('\n');
  if (lines[0]?.trim() !== '---') return null;
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (end < 0) return null;
  const front = lines.slice(1, end);
  const nameIndex = front.findIndex((line) => /^name\s*:/.test(line));
  if (nameIndex >= 0) front[nameIndex] = `name: ${name}`;
  else front.unshift(`name: ${name}`);
  return ['---', ...front, '---', ...lines.slice(end + 1)].join('\n');
}

function formatRevisionTime(value: string, language: 'zh' | 'en'): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date);
}

export default function SkillsHubPage() {
  const { t, language } = useAppI18n();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const previewAbortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const revisionAbortRef = useRef<AbortController | null>(null);
  const historyGenerationRef = useRef(0);
  const [query, setQuery] = useState('');
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<SkillSourceFilter>('builtin');
  const [previewName, setPreviewName] = useState<string | null>(null);
  const [previewDetail, setPreviewDetail] = useState<AgentSkillDetail | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewMode, setPreviewMode] = useState<SkillPreviewMode>('rendered');
  const [editorTarget, setEditorTarget] = useState<SkillEditorTarget | null>(null);
  const [editorName, setEditorName] = useState('');
  const [editorContent, setEditorContent] = useState(EMPTY_SKILL_DOCUMENT);
  const [editorBaseContent, setEditorBaseContent] = useState('');
  const [editorLoading, setEditorLoading] = useState(false);
  const [editorSaving, setEditorSaving] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [historyName, setHistoryName] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<SkillRevision[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [revisionDetail, setRevisionDetail] = useState<SkillRevisionDetail | null>(null);
  const [revisionLoading, setRevisionLoading] = useState(false);
  const [reverting, setReverting] = useState(false);

  const fetchSkills = useCallback(async () => {
    const agentResponse = await skillHubApi.listAgent();
    setAgentSkills(agentResponse.skills);
    setLoadError(null);
  }, []);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('skills.title')} · QwenPaw Data`;
    void fetchSkills()
      .catch((error) => setLoadError(errorMessage(error)))
      .finally(() => setLoading(false));
    return () => {
      document.title = previousTitle;
    };
  }, [fetchSkills, t]);

  useEffect(
    () => () => {
      historyGenerationRef.current += 1;
      previewAbortRef.current?.abort();
      historyAbortRef.current?.abort();
      revisionAbortRef.current?.abort();
    },
    [],
  );

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

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleSkills = useMemo(
    () => agentSkills.filter((skill) => matchesSearch(skill, normalizedQuery)),
    [agentSkills, normalizedQuery],
  );
  const visibleBuiltinSkills = useMemo(
    () => visibleSkills.filter((skill) => skill.source === 'builtin'),
    [visibleSkills],
  );
  const visibleCustomSkills = useMemo(
    () => visibleSkills.filter((skill) => skill.source !== 'builtin'),
    [visibleSkills],
  );
  const visibleSourceSkills =
    sourceFilter === 'builtin' ? visibleBuiltinSkills : visibleCustomSkills;

  const runAction = useCallback(
    async (key: string, action: () => Promise<unknown>, success: string) => {
      setPendingAction(key);
      try {
        await action();
        await fetchSkills();
        message.success(success);
      } catch (error) {
        message.error(t('common.operationFailed', { message: errorMessage(error) }));
      } finally {
        setPendingAction(null);
      }
    },
    [fetchSkills, t],
  );

  const importZip = useCallback(
    async (file: File) => {
      if (!file.name.toLocaleLowerCase().endsWith('.zip')) {
        message.warning(t('skills.zipRequired'));
        return;
      }
      setUploading(true);
      try {
        const { imported } = await skillHubApi.importAgent(file);
        await fetchSkills();
        message.success(t('skills.imported', { count: imported.length }));
      } catch (error) {
        message.error(t('skills.importFailed', { message: errorMessage(error) }));
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    },
    [fetchSkills, t],
  );

  const closePreview = useCallback(() => {
    previewAbortRef.current?.abort();
    previewAbortRef.current = null;
    setPreviewName(null);
    setPreviewDetail(null);
    setPreviewError(null);
    setPreviewLoading(false);
  }, []);

  const closeEditor = useCallback(() => {
    setEditorTarget(null);
    setEditorName('');
    setEditorContent(EMPTY_SKILL_DOCUMENT);
    setEditorBaseContent('');
    setEditorLoading(false);
    setEditorSaving(false);
    setEditorError(null);
  }, []);

  const closeHistory = useCallback(() => {
    historyGenerationRef.current += 1;
    historyAbortRef.current?.abort();
    historyAbortRef.current = null;
    revisionAbortRef.current?.abort();
    revisionAbortRef.current = null;
    setHistoryName(null);
    setRevisions([]);
    setHistoryError(null);
    setHistoryLoading(false);
    setRevisionDetail(null);
    setRevisionLoading(false);
    setReverting(false);
  }, []);

  const openPreview = useCallback((name: string) => {
    previewAbortRef.current?.abort();
    const controller = new AbortController();
    previewAbortRef.current = controller;
    setEditorTarget(null);
    setPreviewName(name);
    setPreviewDetail(null);
    setPreviewError(null);
    setPreviewLoading(true);
    setPreviewMode('rendered');

    void skillHubApi
      .getAgent(name, 'default', controller.signal)
      .then((skill) => {
        if (!controller.signal.aborted) setPreviewDetail(skill);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setPreviewError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreviewLoading(false);
      });
  }, []);

  const openCreate = useCallback(() => {
    closePreview();
    setEditorTarget({ mode: 'create' });
    setEditorName('');
    setEditorContent(EMPTY_SKILL_DOCUMENT);
    setEditorBaseContent('');
    setEditorLoading(false);
    setEditorError(null);
  }, [closePreview]);

  const openEdit = useCallback(
    (name: string) => {
      closePreview();
      const controller = new AbortController();
      previewAbortRef.current = controller;
      setEditorTarget({ mode: 'edit', name });
      setEditorName(name);
      setEditorContent(EMPTY_SKILL_DOCUMENT);
      setEditorBaseContent('');
      setEditorError(null);
      setEditorLoading(true);
      void skillHubApi
        .getAgent(name, 'default', controller.signal)
        .then((skill) => {
          if (controller.signal.aborted) return;
          setEditorName(skill.name);
          setEditorContent(skill.content);
          setEditorBaseContent(skill.content);
        })
        .catch((error) => {
          if (!controller.signal.aborted) setEditorError(errorMessage(error));
        })
        .finally(() => {
          if (!controller.signal.aborted) setEditorLoading(false);
        });
    },
    [closePreview],
  );

  const loadHistory = useCallback((name: string) => {
    historyAbortRef.current?.abort();
    revisionAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    setRevisions([]);
    setRevisionDetail(null);
    setHistoryError(null);
    setHistoryLoading(true);
    void skillHubApi
      .listRevisions(name, 'default', controller.signal)
      .then((response) => {
        if (!controller.signal.aborted) setRevisions(response.revisions);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setHistoryError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false);
      });
  }, []);

  const openHistory = useCallback(
    (name: string) => {
      closePreview();
      setEditorTarget(null);
      historyGenerationRef.current += 1;
      setHistoryName(name);
      loadHistory(name);
    },
    [closePreview, loadHistory],
  );

  const openRevision = useCallback(
    (revisionNo: number) => {
      if (!historyName) return;
      revisionAbortRef.current?.abort();
      const controller = new AbortController();
      revisionAbortRef.current = controller;
      setRevisionDetail(null);
      setRevisionLoading(true);
      void skillHubApi
        .getRevision(historyName, revisionNo, 'default', controller.signal)
        .then((detail) => {
          if (!controller.signal.aborted) setRevisionDetail(detail);
        })
        .catch((error) => {
          if (!controller.signal.aborted) {
            message.error(t('skills.revisionFailed', { message: errorMessage(error) }));
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setRevisionLoading(false);
        });
    },
    [historyName, t],
  );

  const revertRevision = useCallback(
    async (revisionNo: number) => {
      if (!historyName) return;
      const historyGeneration = historyGenerationRef.current;
      const targetName = historyName;
      setReverting(true);
      try {
        await skillHubApi.revertRevision(targetName, revisionNo);
        if (historyGenerationRef.current === historyGeneration) {
          message.success(t('skills.reverted', { revision: revisionNo }));
          loadHistory(targetName);
        }
        await fetchSkills();
      } catch (error) {
        if (historyGenerationRef.current === historyGeneration) {
          message.error(t('common.operationFailed', { message: errorMessage(error) }));
        }
      } finally {
        if (historyGenerationRef.current === historyGeneration) {
          setReverting(false);
        }
      }
    },
    [fetchSkills, historyName, loadHistory, t],
  );

  const changeEditorName = useCallback(
    (value: string) => {
      if (editorTarget?.mode === 'edit') return;
      setEditorName(value);
      setEditorContent((current) => replaceFrontmatterName(current, value) ?? current);
    },
    [editorTarget?.mode],
  );

  const saveEditor = useCallback(async () => {
    if (!editorTarget) return;
    const name = editorName.trim();
    if (!name) {
      message.warning(t('skills.nameRequired'));
      return;
    }
    if (!editorContent.trim()) {
      message.warning(t('skills.contentRequired'));
      return;
    }
    const content = replaceFrontmatterName(editorContent, name);
    if (content === null) {
      message.warning(t('skills.frontmatterRequired'));
      return;
    }
    setEditorSaving(true);
    try {
      if (editorTarget.mode === 'create') {
        await skillHubApi.createAgent(name, content);
        message.success(t('skills.created', { name }));
      } else {
        const currentName = editorTarget.name;
        if (!currentName) throw new Error(t('skills.nameRequired'));
        if (name !== currentName) {
          message.warning(t('skills.renameForbidden'));
          return;
        }
        await skillHubApi.saveAgent(currentName, content, editorBaseContent);
        message.success(t('skills.saved', { name }));
      }
      closeEditor();
      try {
        await fetchSkills();
      } catch (error) {
        setLoadError(errorMessage(error));
      }
    } catch (error) {
      message.error(t('common.operationFailed', { message: errorMessage(error) }));
    } finally {
      setEditorSaving(false);
    }
  }, [closeEditor, editorBaseContent, editorContent, editorName, editorTarget, fetchSkills, t]);

  const copyPreviewSource = useCallback(async () => {
    if (!previewDetail?.content) return;
    try {
      await navigator.clipboard.writeText(previewDetail.content);
      message.success(t('skills.sourceCopied'));
    } catch {
      message.error(t('skills.copyFailed'));
    }
  }, [previewDetail, t]);

  const originLabel = useCallback(
    (origin: string) => {
      const key = ORIGIN_LABEL_KEYS[origin];
      return key ? t(key) : origin;
    },
    [t],
  );

  const editorRetryName = editorTarget?.name ?? null;

  const skillColumns: TableColumnsType<AgentSkill> = [
    {
      title: t('skills.name'),
      dataIndex: 'name',
      width: 260,
      align: 'left',
      render: (_, skill) => (
        <div className={styles.skillIdentity}>
          <span className={styles.skillIcon}>
            <ThunderboltOutlined />
          </span>
          <div>
            <strong title={skill.name}>{skill.name}</strong>
          </div>
        </div>
      ),
    },
    {
      title: t('skills.type'),
      dataIndex: 'source',
      width: 100,
      align: 'left',
      render: (source: string) => (
        <span className={styles.sourceCell}>
          {source === 'builtin' ? t('skills.builtin') : t('skills.custom')}
        </span>
      ),
    },
    {
      title: t('skills.tableDescription'),
      dataIndex: 'description',
      align: 'left',
      render: (description: string) => (
        <span className={styles.descriptionCell}>{description || t('skills.noDescription')}</span>
      ),
    },
    {
      title: t('skills.group'),
      dataIndex: 'group',
      width: 160,
      align: 'left',
      render: (group: string) => (
        <span className={styles.metaCell}>{group || t('skills.custom')}</span>
      ),
    },
    {
      title: t('skills.status'),
      width: 150,
      align: 'left',
      render: (_, skill) => {
        const actionKey = `agent:${skill.name}`;
        return (
          <label className={styles.statusControl}>
            <Switch
              size="small"
              checked={skill.enabled}
              loading={pendingAction === actionKey}
              onChange={(enabled) =>
                void runAction(
                  actionKey,
                  () => skillHubApi.setAgentEnabled(skill.name, enabled),
                  enabled
                    ? t('skills.enableSuccess', { name: skill.name })
                    : t('skills.disableSuccess', { name: skill.name }),
                )
              }
            />
            <span>{skill.enabled ? t('skills.active') : t('skills.inactive')}</span>
          </label>
        );
      },
    },
    {
      title: t('skills.actions'),
      key: 'actions',
      width: 260,
      fixed: 'right',
      align: 'left',
      render: (_, skill) => {
        const actionKey = `agent:${skill.name}`;
        const actionLoading = pendingAction === actionKey;
        const isBuiltin = skill.source === 'builtin';
        return (
          <div className={styles.tableActions}>
            <Button type="text" size="small" onClick={() => openPreview(skill.name)}>
              {t('skills.preview')}
            </Button>
            <Button type="text" size="small" onClick={() => openHistory(skill.name)}>
              {t('skills.history')}
            </Button>
            <Tooltip title={isBuiltin ? t('skills.builtinReadonly') : undefined}>
              <span>
                <Button
                  type="text"
                  size="small"
                  disabled={isBuiltin}
                  onClick={() => openEdit(skill.name)}
                >
                  {t('common.edit')}
                </Button>
              </span>
            </Tooltip>
            {!isBuiltin ? (
              <Popconfirm
                title={t('skills.uninstallTitle')}
                description={t('skills.uninstallDescription')}
                okText={t('skills.uninstall')}
                cancelText={t('common.cancel')}
                onConfirm={() =>
                  runAction(
                    actionKey,
                    () => skillHubApi.uninstallAgent(skill.name),
                    t('skills.uninstalled', { name: skill.name }),
                  )
                }
              >
                <Button danger type="text" size="small" loading={actionLoading}>
                  {t('skills.uninstall')}
                </Button>
              </Popconfirm>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('skills.title')} description={t('skills.description')} />

      <main className={styles.main}>
        <section className={styles.toolbar} aria-label={t('skills.filterActions')}>
          <Segmented<SkillSourceFilter>
            className={styles.sourceTabs}
            aria-label={t('skills.typeFilter')}
            value={sourceFilter}
            options={[
              {
                value: 'builtin',
                label: `${t('skills.builtinGroup')}（${visibleBuiltinSkills.length}）`,
              },
              {
                value: 'custom',
                label: `${t('skills.customGroup')}（${visibleCustomSkills.length}）`,
              },
            ]}
            onChange={setSourceFilter}
          />
          <Input.Search
            allowClear
            className={styles.search}
            placeholder={t('skills.search')}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Button
            aria-label={t('common.refresh')}
            title={t('common.refresh')}
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={() => void refresh()}
          />
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
          <Dropdown
            trigger={['click']}
            menu={{
              items: [
                { key: 'create', icon: <PlusOutlined />, label: t('skills.create') },
                {
                  key: 'upload-zip',
                  icon: <CloudUploadOutlined />,
                  label: t('skills.importZip'),
                  disabled: uploading,
                },
              ],
              onClick: ({ key }: { key: string }) => {
                if (key === 'create') openCreate();
                if (key === 'upload-zip') fileInputRef.current?.click();
              },
            }}
          >
            <Button type="primary" icon={<PlusOutlined />} loading={uploading}>
              {t('skills.addSkill')}
            </Button>
          </Dropdown>
        </section>

        {loading ? (
          <div className={styles.state}>
            <Spin />
            <span>{t('skills.loading')}</span>
          </div>
        ) : null}
        {!loading && loadError ? (
          <Alert
            showIcon
            type="error"
            message={t('skills.loadFailed')}
            description={loadError}
            action={
              <Button size="small" onClick={() => void refresh()}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : null}

        {!loading && !loadError && !visibleSkills.length ? (
          <div className={styles.empty}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={normalizedQuery ? t('skills.noMatch') : t('skills.agentEmpty')}
            />
          </div>
        ) : null}

        {!loading && !loadError && visibleSkills.length ? (
          <section className={styles.skillCollection} aria-label={t('skills.agentSkills')}>
            <Table<AgentSkill>
              className={styles.table}
              rowKey="name"
              columns={skillColumns}
              dataSource={[...visibleSourceSkills]}
              pagination={false}
              sticky
              scroll={{ x: 1080 }}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('skills.noMatch')} />
                ),
              }}
            />
          </section>
        ) : null}
      </main>

      <RouteDrawer
        width={640}
        placement="right"
        title={
          previewName ? `${previewName} · ${t('skills.previewTitle')}` : t('skills.previewTitle')
        }
        open={Boolean(previewName)}
        destroyOnHidden
        onClose={closePreview}
        classNames={{ body: styles.previewDrawerBody }}
        extra={
          <Button
            type="text"
            size="small"
            icon={<CopyOutlined />}
            disabled={!previewDetail?.content}
            onClick={() => void copyPreviewSource()}
          >
            {t('skills.copySource')}
          </Button>
        }
      >
        {previewLoading ? (
          <div className={styles.previewState}>
            <Spin />
            <span>{t('skills.loadingPreview')}</span>
          </div>
        ) : null}
        {!previewLoading && previewError ? (
          <Alert
            showIcon
            type="error"
            message={t('skills.previewFailed')}
            description={previewError}
            action={
              previewName ? (
                <Button size="small" onClick={() => openPreview(previewName)}>
                  {t('common.retry')}
                </Button>
              ) : null
            }
          />
        ) : null}
        {!previewLoading && !previewError && previewDetail ? (
          <div className={styles.skillPreview}>
            <div className={styles.previewMeta}>
              <div>
                <strong>{previewDetail.name}</strong>
                <Tag color={previewDetail.source === 'builtin' ? 'blue' : 'default'}>
                  {previewDetail.source === 'builtin' ? t('skills.builtin') : t('skills.custom')}
                </Tag>
                <Tag color={previewDetail.enabled ? 'success' : 'default'}>
                  {previewDetail.enabled ? t('common.enabled') : t('common.disabled')}
                </Tag>
              </div>
              <p>{previewDetail.description || t('skills.noDescription')}</p>
            </div>
            <Segmented<SkillPreviewMode>
              block
              value={previewMode}
              options={[
                { value: 'rendered', label: t('skills.rendered') },
                { value: 'source', label: t('skills.source') },
              ]}
              onChange={setPreviewMode}
            />
            <div className={styles.previewContent}>
              {previewMode === 'rendered' ? (
                <Markdown
                  className={styles.previewMarkdown}
                  content={stripSkillFrontmatter(previewDetail.content)}
                  baseFontSize={13}
                  baseLineHeight={1.7}
                />
              ) : (
                <pre className={styles.previewSource}>{previewDetail.content}</pre>
              )}
            </div>
          </div>
        ) : null}
      </RouteDrawer>

      <RouteDrawer
        width={720}
        placement="right"
        title={
          historyName ? `${historyName} · ${t('skills.historyTitle')}` : t('skills.historyTitle')
        }
        open={Boolean(historyName)}
        destroyOnHidden
        onClose={closeHistory}
        classNames={{ body: styles.previewDrawerBody }}
      >
        {historyLoading ? (
          <div className={styles.previewState}>
            <Spin />
            <span>{t('skills.loadingHistory')}</span>
          </div>
        ) : null}
        {!historyLoading && historyError ? (
          <Alert
            showIcon
            type="error"
            message={t('skills.historyFailed')}
            description={historyError}
            action={
              historyName ? (
                <Button size="small" onClick={() => loadHistory(historyName)}>
                  {t('common.retry')}
                </Button>
              ) : null
            }
          />
        ) : null}
        {!historyLoading && !historyError && !revisions.length ? (
          <div className={styles.empty}>
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('skills.historyEmpty')} />
          </div>
        ) : null}
        {!historyLoading && !historyError && revisions.length ? (
          <div className={styles.historyLayout}>
            <div className={styles.revisionList}>
              {revisions.map((revision, index) => (
                <article
                  className={`${styles.revisionItem} ${
                    revisionDetail?.revision_no === revision.revision_no
                      ? styles.revisionItemActive
                      : ''
                  }`}
                  key={revision.revision_no}
                  onClick={() => openRevision(revision.revision_no)}
                >
                  <div className={styles.revisionHead}>
                    <strong>{t('skills.revisionLabel', { revision: revision.revision_no })}</strong>
                    <Tag color={index === 0 ? 'blue' : 'default'}>
                      {originLabel(revision.origin)}
                    </Tag>
                    {index === 0 ? <Tag color="success">{t('skills.revisionCurrent')}</Tag> : null}
                    <span>{formatRevisionTime(revision.created_at, language)}</span>
                  </div>
                  {revision.note ? <p>{revision.note}</p> : null}
                  {index > 0 ? (
                    <Popconfirm
                      title={t('skills.revertTitle')}
                      description={t('skills.revertDescription')}
                      okText={t('skills.revert')}
                      cancelText={t('common.cancel')}
                      onConfirm={() => revertRevision(revision.revision_no)}
                    >
                      <Button
                        size="small"
                        loading={reverting}
                        onClick={(event) => event.stopPropagation()}
                      >
                        {t('skills.revert')}
                      </Button>
                    </Popconfirm>
                  ) : null}
                </article>
              ))}
            </div>
            <div className={styles.revisionDetail}>
              {revisionLoading ? (
                <div className={styles.previewState}>
                  <Spin />
                  <span>{t('skills.loadingRevision')}</span>
                </div>
              ) : null}
              {!revisionLoading && !revisionDetail ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={t('skills.revisionHint')}
                />
              ) : null}
              {!revisionLoading && revisionDetail ? (
                <div className={styles.revisionFiles}>
                  {revisionDetail.files.map((file) => (
                    <section key={file.relative_path}>
                      <header>{file.relative_path}</header>
                      <pre className={styles.previewSource}>{file.content}</pre>
                    </section>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </RouteDrawer>

      <RouteDrawer
        width={720}
        placement="right"
        title={t(editorTarget?.mode === 'edit' ? 'skills.editorEdit' : 'skills.editorCreate')}
        open={Boolean(editorTarget)}
        destroyOnHidden
        onClose={closeEditor}
        classNames={{ body: styles.editorDrawerBody }}
        footer={
          <div className={styles.editorFooter}>
            <Button onClick={closeEditor}>{t('common.cancel')}</Button>
            <Button
              type="primary"
              loading={editorSaving}
              disabled={editorLoading || Boolean(editorError)}
              onClick={() => void saveEditor()}
            >
              {t('common.save')}
            </Button>
          </div>
        }
      >
        {editorLoading ? (
          <div className={styles.previewState}>
            <Spin />
            <span>{t('skills.loadingPreview')}</span>
          </div>
        ) : null}
        {!editorLoading && editorError ? (
          <Alert
            showIcon
            type="error"
            message={t('skills.previewFailed')}
            description={editorError}
            action={
              editorRetryName ? (
                <Button size="small" onClick={() => openEdit(editorRetryName)}>
                  {t('common.retry')}
                </Button>
              ) : null
            }
          />
        ) : null}
        {!editorLoading && !editorError && editorTarget ? (
          <div className={styles.editorForm}>
            <label className={styles.editorField}>
              <span>{t('skills.name')}</span>
              <Input
                value={editorName}
                disabled={editorTarget.mode === 'edit'}
                placeholder={t('skills.namePlaceholder')}
                onChange={(event) => changeEditorName(event.target.value)}
              />
              {editorTarget.mode === 'edit' ? (
                <span className={styles.editorHint}>{t('skills.renameForbidden')}</span>
              ) : null}
            </label>
            <label className={`${styles.editorField} ${styles.editorContent}`}>
              <span>{t('skills.content')}</span>
              <Input.TextArea
                value={editorContent}
                spellCheck={false}
                placeholder={t('skills.contentPlaceholder')}
                onChange={(event) => setEditorContent(event.target.value)}
              />
            </label>
          </div>
        ) : null}
      </RouteDrawer>
    </div>
  );
}
