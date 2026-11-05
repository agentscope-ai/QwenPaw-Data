import {
  DatabaseOutlined,
  LoadingOutlined,
  ReloadOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Button, Input, Select, Tooltip } from '@agentscope-ai/design';
import { Empty, Spin } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ChannelIcon from '@/features/console/components/ChannelIcon';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import OverflowTooltip from '@/features/console/components/OverflowTooltip';
import SessionActions from '@/features/console/components/SessionActions';
import {
  formatSessionUpdatedAt,
  selectCatalogSessions,
  type SessionCatalogSort,
  type SessionCatalogStatus,
} from '@/features/console/model/sessionCatalog';
import {
  buildConsoleSessionPath,
  buildNewConsoleSessionPath,
} from '@/features/console/model/sessionNavigation';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import styles from './index.module.less';

export default function SessionsPage() {
  const navigate = useNavigate();
  const { consoleController } = useConsoleLayout();
  const [searchText, setSearchText] = useState('');
  const [status, setStatus] = useState<SessionCatalogStatus>('all');
  const [datasourceId, setDatasourceId] = useState('all');
  const [sort, setSort] = useState<SessionCatalogSort>('updated_desc');
  const {
    sessions,
    datasources,
    connectionStatus,
    error: loadError,
  } = consoleController;
  const loading = connectionStatus === 'loading';

  useEffect(() => {
    const previousTitle = document.title;
    document.title = '全部会话 · QwenPaw Data';
    return () => {
      document.title = previousTitle;
    };
  }, []);

  const visibleSessions = useMemo(() => selectCatalogSessions(sessions, {
    searchText,
    status,
    datasourceId,
    sort,
  }), [datasourceId, searchText, sessions, sort, status]);

  const datasourceNames = useMemo(
    () => new Map(datasources.map((datasource) => [datasource.id, datasource.name])),
    [datasources],
  );

  const openSession = useCallback((sessionId: string) => {
    navigate(buildConsoleSessionPath(sessionId));
  }, [navigate]);

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    await consoleController.renameSession(sessionId, title);
  }, [consoleController]);

  const deleteSession = useCallback(async (sessionId: string) => {
    await consoleController.deleteSession(sessionId);
  }, [consoleController]);

  return (
    <div className={styles.content}>
      <ConsoleRouteHeader
        parentLabel="数据会话"
        title="全部会话"
        onParentClick={() => navigate(buildNewConsoleSessionPath())}
      />

      <main className={styles.main}>
        <div className={styles.intro}>
          <p>继续进行中的分析，或回看历史对话、执行轨迹与成果。</p>
          <span>{visibleSessions.length} 个会话</span>
        </div>

        <section className={styles.filters} aria-label="会话筛选">
          <Input
            className={styles.search}
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索会话标题"
            aria-label="搜索会话标题"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
          <Select
            aria-label="筛选会话状态"
            value={status}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'running', label: '执行中' },
              { value: 'idle', label: '已完成' },
            ]}
            onChange={(value) => setStatus(value as SessionCatalogStatus)}
          />
          <Select
            aria-label="筛选数据源"
            value={datasourceId}
            options={[
              { value: 'all', label: '全部数据源' },
              ...datasources.map((datasource) => ({
                value: datasource.id,
                label: datasource.name,
              })),
            ]}
            onChange={(value) => setDatasourceId(value)}
          />
          <Select
            aria-label="会话排序"
            value={sort}
            options={[
              { value: 'updated_desc', label: '最近更新' },
              { value: 'updated_asc', label: '最早更新' },
              { value: 'chat_count_desc', label: '轮次最多' },
            ]}
            onChange={(value) => setSort(value as SessionCatalogSort)}
          />
        </section>

        <section className={styles.catalog} aria-label="全部数据会话">
          <div className={styles.tableHeader}>
            <span>会话</span>
            <span>数据源</span>
            <span>轮次</span>
            <span>最近更新</span>
            <span aria-hidden="true" />
          </div>

          {loading ? (
            <div className={styles.state}>
              <Spin />
              <span>正在加载会话…</span>
            </div>
          ) : null}

          {!loading && loadError ? (
            <div className={styles.state}>
              <strong>会话加载失败</strong>
              <span>{loadError}</span>
              <Button
                icon={<ReloadOutlined />}
                onClick={consoleController.reloadSessions}
              >
                重新加载
              </Button>
            </div>
          ) : null}

          {!loading && !loadError && !visibleSessions.length ? (
            <div className={styles.empty}>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={searchText || status !== 'all' || datasourceId !== 'all'
                  ? '没有符合筛选条件的会话'
                  : '暂无历史会话'}
              />
            </div>
          ) : null}

          {!loading && !loadError ? visibleSessions.map((session) => (
            <div className={styles.sessionRow} key={session.id}>
              <button
                className={styles.sessionIdentity}
                type="button"
                onClick={() => openSession(session.id)}
              >
                <span className={`${styles.sessionIcon} ${
                  session.status === 'running' ? styles.sessionIconRunning : ''
                }`}>
                  {session.status === 'running'
                    ? <LoadingOutlined spin />
                    : <ChannelIcon channel={session.channel} />}
                </span>
                <span className={styles.sessionCopy}>
                  <span className={styles.sessionTitleLine}>
                    <OverflowTooltip title={session.title || '未命名会话'}>
                      <strong>{session.title || '未命名会话'}</strong>
                    </OverflowTooltip>
                    {session.status === 'running' ? <em>执行中</em> : null}
                  </span>
                  <OverflowTooltip title={`创建于 ${new Date(session.created_at).toLocaleDateString('zh-CN')}`}>
                    <span>创建于 {new Date(session.created_at).toLocaleDateString('zh-CN')}</span>
                  </OverflowTooltip>
                </span>
              </button>

              <OverflowTooltip
                title={session.datasource_id
                  ? datasourceNames.get(session.datasource_id) ?? session.datasource_id
                  : '未绑定数据源'}
              >
                <span className={styles.datasource}>
                  <DatabaseOutlined />
                  {session.datasource_id
                    ? datasourceNames.get(session.datasource_id) ?? session.datasource_id
                    : '未绑定数据源'}
                </span>
              </OverflowTooltip>
              <span className={styles.rounds}>{session.chat_count} 轮</span>
              <span className={styles.updatedAt}>
                {formatSessionUpdatedAt(session.updated_at)}
              </span>
              <span className={styles.rowActions}>
                <SessionActions
                  className={styles.sessionActions}
                  session={session}
                  onRename={renameSession}
                  onDelete={deleteSession}
                />
                <Tooltip title="前往对话">
                  <button
                    className={styles.openButton}
                    type="button"
                    aria-label={`前往对话：${session.title || '未命名会话'}`}
                    onClick={() => openSession(session.id)}
                  >
                    <RightOutlined />
                  </button>
                </Tooltip>
              </span>
            </div>
          )) : null}
        </section>
      </main>
    </div>
  );
}
