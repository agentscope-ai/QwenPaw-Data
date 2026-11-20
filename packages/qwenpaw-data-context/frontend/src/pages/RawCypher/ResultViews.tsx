import { Card, Tabs } from '@/design';
import { Descriptions, Table as AntTable, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { RawCypherResponse } from '@/services/rawCypher';

const { Text } = Typography;

interface ResultViewsProps {
  result: RawCypherResponse;
  compact?: boolean;
}

function renderCell(value: unknown): string {
  if (value == null) return '-';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export default function ResultViews({ result, compact = false }: ResultViewsProps) {
  const { t } = useTranslation();
  const columns = useMemo<ColumnsType<Record<string, unknown>>>(
    () =>
      result.columns.map((column) => ({
        title: column,
        dataIndex: column,
        key: column,
        ellipsis: true,
        render: (value: unknown) => (
          <Text
            copyable={{ text: renderCell(value) }}
            ellipsis
            style={{ maxWidth: compact ? 240 : 360 }}
          >
            {renderCell(value)}
          </Text>
        ),
      })),
    [compact, result.columns],
  );

  const tableRows = useMemo(
    () => result.rows.map((row, index) => ({ ...row, __rowKey: index })),
    [result.rows],
  );

  const body = (
    <>
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 3 }}
        style={{ marginBottom: compact ? 8 : 16 }}
      >
        <Descriptions.Item label={t('rawCypher.result.rows')}>{result.count}</Descriptions.Item>
        <Descriptions.Item label={t('rawCypher.result.columns')}>
          {result.columns.length}
        </Descriptions.Item>
        <Descriptions.Item label={t('rawCypher.result.elapsed')}>
          {result.elapsed_ms} ms
        </Descriptions.Item>
      </Descriptions>
      <Text type="secondary">{t('rawCypher.result.countHint')}</Text>
      <Tabs
        style={{ marginTop: compact ? 8 : 12 }}
        items={[
          {
            key: 'table',
            label: t('rawCypher.result.table'),
            children: (
              <AntTable
                rowKey="__rowKey"
                columns={columns}
                dataSource={tableRows}
                pagination={{ pageSize: compact ? 10 : 20, showSizeChanger: !compact }}
                scroll={{ x: 'max-content', y: compact ? 220 : undefined }}
                size="small"
              />
            ),
          },
          {
            key: 'json',
            label: t('rawCypher.result.json'),
            children: (
              <pre
                style={{
                  maxHeight: compact ? 220 : 520,
                  margin: 0,
                  padding: compact ? 10 : 16,
                  overflow: 'auto',
                  background: '#f6f8fa',
                  borderRadius: 8,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {JSON.stringify(result, null, 2)}
              </pre>
            ),
          },
        ]}
      />
    </>
  );

  if (compact) return <div>{body}</div>;

  return (
    <Card className="context-page-surface" title={t('rawCypher.result.title')}>
      {body}
    </Card>
  );
}
