import {
  DatabaseOutlined,
  LoadingOutlined,
  ReloadOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Button, Input, Select, Tooltip } from '@agentscope-ai/design';
import { Alert, Empty, Table, type TableColumnsType } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useNavigate } from 'react-router-dom';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import type { ConsoleSession } from '@/features/console/api/types';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader/index';
import OverflowTooltip from '@/features/console/components/OverflowTooltip';
import SessionActions from '@/features/console/components/SessionActions';
import {
  formatSessionUpdatedAt,
  type SessionCatalogSort,
  type SessionCatalogStatus,
} from '@/features/sessions/model/sessionCatalog';
import { buildConsoleSessionPath } from '@/shared/navigation/sessionNavigation';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';
import { startVisiblePolling } from '@/features/console/model/polling/visiblePolling';
import { dataConsoleApi } from '@/features/console/api/client';
import { getHostAuthContext, subscribeHostAuth } from '@/services/auth';

export default function SessionsPage() {
  const navigate = useNavigate();
  const tableRef = useRef<HTMLElement>(null);
  const [tableHeight, setTableHeight] = useState(400);
  useEffect(() => {
    const element = tableRef.current;
    if (!element) return;
    const resize = () => {
      const header = element.querySelector<HTMLElement>('.ant-table-header');
      const pager = element.querySelector<HTMLElement>('.ant-pagination');
      const pagerStyle = pager ? getComputedStyle(pager) : null;
      const pagerHeight = pager
        ? pager.offsetHeight +
          parseFloat(pagerStyle!.marginTop) +
          parseFloat(pagerStyle!.marginBottom)
        : 64;
      setTableHeight(
        Math.max(120, element.clientHeight - (header?.offsetHeight || 48) - pagerHeight),
      );
    };
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    return () => observer.disconnect();
  }, []);
  const { consoleController } = useConsoleLayout();
  const { language, t } = useAppI18n();
  const [searchText, setSearchText] = useState('');
  const [status, setStatus] = useState<SessionCatalogStatus>('all');
  const [datasourceId, setDatasourceId] = useState('all');
  const [sort, setSort] = useState<SessionCatalogSort>('updated_desc');
  const { datasources } = consoleController;
  const auth = useSyncExternalStore(subscribeHostAuth, getHostAuthContext);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [search, setSearch] = useState('');
  const [version, setVersion] = useState(0);
  const [result, setResult] = useState<{
    items: ConsoleSession[];
    total: number;
    revision: number;
  }>({ items: [], total: 0, revision: auth.revision });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const reload = useCallback(() => setVersion((value) => value + 1), []);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchText.trim());
      setPage(1);
    }, 250);
    return () => clearTimeout(timer);
  }, [searchText]);
  useEffect(() => {
    const abort = new AbortController();
    if (!auth.ready) return;
    let pending = false;
    const load = async (background = false) => {
      if (pending || abort.signal.aborted) return;
      pending = true;
      if (!background) setLoading(true);
      setLoadError(null);
      await dataConsoleApi
        .listSessions(
          {
            page,
            page_size: pageSize,
            min_chat_count: 1,
            search_text: search || undefined,
            status: status === 'all' ? undefined : status,
            datasource_id: datasourceId === 'all' ? undefined : datasourceId,
            sort,
          },
          abort.signal,
        )
        .then((next) => {
          if (abort.signal.aborted || auth.revision !== getHostAuthContext().revision) return;
          const lastPage = Math.max(1, Math.ceil(next.total / pageSize));
          if (page > lastPage) {
            setPage(lastPage);
            return;
          }
          setResult((previous) =>
            previous.revision === auth.revision &&
            previous.total === next.total &&
            JSON.stringify(previous.items) === JSON.stringify(next.items)
              ? previous
              : { ...next, revision: auth.revision },
          );
        })
        .catch((error) => {
          if (!abort.signal.aborted)
            setLoadError(error instanceof Error ? error.message : String(error));
        })
        .finally(() => {
          pending = false;
          if (!abort.signal.aborted) setLoading(false);
        });
    };
    void load();
    const stopPolling = startVisiblePolling(() => load(true), 30_000, document, false);
    return () => {
      stopPolling();
      abort.abort();
    };
  }, [page, pageSize, search, status, datasourceId, sort, version, auth.ready, auth.revision]);
  const visibleSessions = auth.ready && result.revision === auth.revision ? result.items : [];

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('sessions.pageTitle')} · QwenPaw Data`;
    return () => {
      document.title = previousTitle;
    };
  }, [t]);

  const datasourceNames = useMemo(
    () => new Map(datasources.map((datasource) => [datasource.id, datasource.name])),
    [datasources],
  );

  const openSession = useCallback(
    (sessionId: string) => {
      navigate(buildConsoleSessionPath(sessionId));
    },
    [navigate],
  );

  const rename = consoleController.renameSession;
  const remove = consoleController.deleteSession;
  const renameSession = useCallback(
    async (sessionId: string, title: string) => {
      await rename(sessionId, title);
      reload();
    },
    [rename, reload],
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      await remove(sessionId);
      reload();
    },
    [remove, reload],
  );

  const columns = useMemo<TableColumnsType<ConsoleSession>>(
    () => [
      {
        title: t('sessions.session'),
        dataIndex: 'title',
        width: 340,
        render: (_, session) => (
          <button
            className={styles.sessionIdentity}
            type="button"
            onClick={() => openSession(session.id)}
          >
            <span
              className={`${styles.sessionIcon} ${
                session.status === 'running' ? styles.sessionIconRunning : ''
              }`}
            >
              {session.status === 'running' ? (
                <LoadingOutlined spin />
              ) : (
                <ChannelIcon channel={session.channel} />
              )}
            </span>
            <span className={styles.sessionCopy}>
              <span className={styles.sessionTitleLine}>
                <OverflowTooltip title={session.title || t('common.unnamedSession')}>
                  <strong>{session.title || t('common.unnamedSession')}</strong>
                </OverflowTooltip>
                {session.status === 'running' ? <em>{t('sessions.executing')}</em> : null}
              </span>
              <OverflowTooltip
                title={t('sessions.createdAt', {
                  date: new Date(session.created_at).toLocaleDateString(
                    language === 'zh' ? 'zh-CN' : 'en-US',
                  ),
                })}
              >
                <span>
                  {t('sessions.createdAt', {
                    date: new Date(session.created_at).toLocaleDateString(
                      language === 'zh' ? 'zh-CN' : 'en-US',
                    ),
                  })}
                </span>
              </OverflowTooltip>
            </span>
          </button>
        ),
      },
      {
        title: t('sessions.datasource'),
        dataIndex: 'datasource_id',
        width: 200,
        render: (_, session) => {
          const datasource = session.datasource_id
            ? (datasourceNames.get(session.datasource_id) ?? session.datasource_id)
            : t('sessions.noDatasource');
          return (
            <OverflowTooltip title={datasource}>
              <span className={styles.datasource}>
                <DatabaseOutlined />
                {datasource}
              </span>
            </OverflowTooltip>
          );
        },
      },
      {
        title: t('sessions.rounds'),
        dataIndex: 'chat_count',
        width: 100,
        render: (count: number) => (
          <span className={styles.rounds}>{t('sessions.roundCount', { count })}</span>
        ),
      },
      {
        title: t('sessions.updatedAt'),
        dataIndex: 'updated_at',
        width: 140,
        render: (updatedAt: string) => (
          <span className={styles.updatedAt}>
            {formatSessionUpdatedAt(updatedAt, new Date(), language)}
          </span>
        ),
      },
      {
        title: t('sessions.actions'),
        key: 'actions',
        width: 110,
        fixed: 'right',
        render: (_, session) => (
          <span className={styles.rowActions}>
            <SessionActions
              className={styles.sessionActions}
              session={session}
              onRename={renameSession}
              onDelete={deleteSession}
            />
            <Tooltip title={t('sessions.openChat')}>
              <button
                className={styles.openButton}
                type="button"
                aria-label={`${t('sessions.openChat')}：${session.title || t('common.unnamedSession')}`}
                onClick={() => openSession(session.id)}
              >
                <RightOutlined />
              </button>
            </Tooltip>
          </span>
        ),
      },
    ],
    [t, language, datasourceNames, openSession, renameSession, deleteSession],
  );

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('sessions.pageTitle')} description={t('sessions.description')} />

      <main className={styles.main}>
        <section className={styles.filters} aria-label={t('sessions.filters')}>
          <Input
            className={styles.search}
            allowClear
            prefix={<SearchOutlined />}
            placeholder={t('sessions.searchTitle')}
            aria-label={t('sessions.searchTitle')}
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
          <Select
            aria-label={t('sessions.filterStatus')}
            value={status}
            options={[
              { value: 'all', label: t('sessions.allStatus') },
              { value: 'running', label: t('sessions.executing') },
              { value: 'idle', label: t('sessions.finished') },
            ]}
            onChange={(value) => {
              setStatus(value as SessionCatalogStatus);
              setPage(1);
            }}
          />
          <Select
            aria-label={t('sessions.filterDatasource')}
            value={datasourceId}
            options={[
              { value: 'all', label: t('sessions.allDatasource') },
              ...datasources.map((datasource) => ({
                value: datasource.id,
                label: datasource.name,
              })),
            ]}
            onChange={(value) => {
              setDatasourceId(value);
              setPage(1);
            }}
          />
          <Select
            aria-label={t('sessions.sort')}
            value={sort}
            options={[
              { value: 'updated_desc', label: t('sessions.updatedDesc') },
              { value: 'updated_asc', label: t('sessions.updatedAsc') },
              { value: 'chat_count_desc', label: t('sessions.chatCountDesc') },
            ]}
            onChange={(value) => {
              setSort(value as SessionCatalogSort);
              setPage(1);
            }}
          />
        </section>

        <div className={styles.tableSummary}>
          {t('sessions.count', { count: result.revision === auth.revision ? result.total : 0 })}
        </div>

        {loadError ? (
          <Alert
            className={styles.loadAlert}
            showIcon
            type="error"
            message={t('sessions.loadFailed')}
            description={loadError}
            action={
              <Button icon={<ReloadOutlined />} onClick={reload}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : null}

        <section ref={tableRef} className={styles.tableCard} aria-label={t('sessions.catalog')}>
          <Table<ConsoleSession>
            rowKey="id"
            loading={loading}
            columns={columns}
            dataSource={loadError ? [] : visibleSessions}
            pagination={{
              current: page,
              pageSize,
              total: result.revision === auth.revision ? result.total : 0,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              onChange: (nextPage, nextSize) => {
                setPage(nextSize === pageSize ? nextPage : 1);
                setPageSize(nextSize);
              },
            }}
            tableLayout="fixed"
            scroll={{ x: 890, y: tableHeight }}
            rowClassName={styles.sessionTableRow}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    searchText || status !== 'all' || datasourceId !== 'all'
                      ? t('sessions.noFiltered')
                      : t('nav.noHistorySession')
                  }
                />
              ),
            }}
          />
        </section>
      </main>
    </div>
  );
}
