import { Empty, Pagination, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type {
  BackgroundTaskUsageItem,
  BackgroundTaskUsagePage,
  SessionUsageItem,
  SessionUsagePage,
} from '@/features/console/api/usageTypes';
import { formatUsageDate } from '@/features/console/model/usage/usagePresentation';
import { useAppI18n } from '@/i18n';
import UsageValue from './UsageValue';
import styles from './index.module.less';

type Row = SessionUsageItem | BackgroundTaskUsageItem;
type Props = (
  | { kind: 'sessions'; data: SessionUsagePage }
  | { kind: 'background'; data: BackgroundTaskUsagePage }
) & {
  loading: boolean;
  page: number;
  onPageChange: (page: number) => void;
};

export default function UsageDetails({ kind, data, loading, page, onPageChange }: Props) {
  const { t, language } = useAppI18n();
  const columns: ColumnsType<Row> = [
    {
      title: kind === 'sessions' ? 'Session' : t('usage.source'),
      key: 'identity',
      width: 230,
      render: (_, row) =>
        'session_id' in row ? (
          <div className={styles.identity}>
            <strong>{row.title}</strong>
            <span>{row.session_id}</span>
            {row.session_state === 'deleted' ? <Tag>{t('usage.deleted')}</Tag> : null}
          </div>
        ) : (
          <span className={styles.source}>{row.source}</span>
        ),
    },
    {
      title: t('usage.lastActivity'),
      key: 'activity',
      width: 130,
      render: (_, row) => formatUsageDate(row.last_activity_at, data.window.timezone, language),
    },
    {
      title: t('usage.models'),
      key: 'models',
      width: 200,
      render: (_, row) => (
        <div className={styles.models}>
          {row.models.map((model) => (
            <Tooltip
              key={JSON.stringify([model.provider_id, model.model_id, model.model_type])}
              title={`${model.provider_id} / ${model.model_id} / ${model.model_type}`}
            >
              <Tag
                tabIndex={0}
                aria-label={`${model.provider_id} / ${model.model_id} / ${model.model_type}`}
              >
                {model.model_id}
              </Tag>
            </Tooltip>
          ))}
        </div>
      ),
    },
    {
      title: t('usage.input'),
      key: 'input',
      align: 'right',
      width: 110,
      render: (_, row) => <UsageValue value={row.generation.input_tokens} />,
    },
    {
      title: t('usage.output'),
      key: 'output',
      align: 'right',
      width: 110,
      render: (_, row) => <UsageValue value={row.generation.output_tokens} />,
    },
    {
      title: 'Embedding',
      key: 'embedding',
      align: 'right',
      width: 110,
      render: (_, row) => <UsageValue value={row.embedding.tokens} />,
    },
    {
      title: t('usage.calls'),
      key: 'calls',
      align: 'right',
      width: 160,
      render: (_, row) => (
        <div className={styles.callCounts}>
          <span>
            {t('usage.generation')}: <UsageValue value={row.generation.calls} compact={false} />
          </span>
          <span>
            Embedding: <UsageValue value={row.embedding.calls} compact={false} />
          </span>
        </div>
      ),
    },
  ];
  const count = 'sessions' in data.totals ? data.totals.sessions : data.totals.groups;
  return (
    <>
      <div className={styles.detailCount}>
        {count} {t(kind === 'sessions' ? 'usage.sessionUnit' : 'usage.sourceUnit')}
      </div>
      <Table<Row>
        columns={columns}
        dataSource={data.items}
        size="middle"
        rowKey={(row) => ('session_id' in row ? row.session_id : row.group_id)}
        pagination={false}
        loading={loading}
        scroll={{ x: 1150 }}
        locale={{
          emptyText: (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t(`usage.${kind}.empty`)} />
          ),
        }}
      />
      {data.pagination.page_count > 0 ? (
        <div className={styles.pagination}>
          <Pagination
            current={page}
            pageSize={data.pagination.page_size}
            total={data.pagination.total}
            onChange={onPageChange}
            showSizeChanger={false}
            disabled={loading}
          />
        </div>
      ) : null}
    </>
  );
}
