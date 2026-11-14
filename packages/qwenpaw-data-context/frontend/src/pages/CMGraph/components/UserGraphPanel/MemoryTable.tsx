import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Card,
  Input,
  Pagination,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';

import { message } from '@/design';
import { ApiRequestError, getErrorMessage } from '@/services/apiError';
import type {
  UserGraphMemory,
  UserGraphMemoryCreate,
  UserGraphMemoryPatch,
  UserGraphMemoryStatus,
  UserGraphMemoryType,
} from '@/services/userGraph';
import { formatDateTime } from '@/utils';
import type { UserGraphMemoryQuery } from './useUserGraph';
import MemoryEditorDrawer from './MemoryEditorDrawer';
import type { MemoryDrawerMode } from './MemoryEditorDrawer';

interface MemoryMutation {
  kind: 'create' | 'update' | 'revoke';
  memoryId?: string;
}

interface MemoryTableProps {
  memories: UserGraphMemory[];
  total: number;
  query: UserGraphMemoryQuery;
  loading: boolean;
  error: string | null;
  canWrite: boolean;
  mutation: MemoryMutation | null;
  onQueryChange: (patch: Partial<UserGraphMemoryQuery>, resetPage?: boolean) => void;
  onPageChange: (page: number, pageSize: number) => void;
  onRefresh: () => Promise<void>;
  onLoad: (memoryId: string) => Promise<UserGraphMemory>;
  onCreate: (body: UserGraphMemoryCreate) => Promise<UserGraphMemory>;
  onUpdate: (memoryId: string, body: UserGraphMemoryPatch) => Promise<UserGraphMemory>;
  onRevoke: (memoryId: string) => Promise<UserGraphMemory>;
}

const MEMORY_TYPES: UserGraphMemoryType[] = ['principle', 'preference', 'anchored_rule', 'trigger'];
const MEMORY_STATUSES: UserGraphMemoryStatus[] = ['active', 'needs_review', 'revoked'];

export default function MemoryTable({
  memories,
  total,
  query,
  loading,
  error,
  canWrite,
  mutation,
  onQueryChange,
  onPageChange,
  onRefresh,
  onLoad,
  onCreate,
  onUpdate,
  onRevoke,
}: MemoryTableProps) {
  const { t } = useTranslation();
  const [searchValue, setSearchValue] = useState(query.q ?? '');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<MemoryDrawerMode>('view');
  const [selectedMemory, setSelectedMemory] = useState<UserGraphMemory | null>(null);

  useEffect(() => {
    setSearchValue(query.q ?? '');
  }, [query.q]);

  const openDrawer = (mode: MemoryDrawerMode, memory: UserGraphMemory | null = null) => {
    setDrawerMode(mode);
    setSelectedMemory(memory);
    setDrawerOpen(true);
  };

  const handleRevoke = async (memory: UserGraphMemory) => {
    try {
      await onRevoke(memory.id);
      message.success(t('kgBrowser.userGraph.memoryRevoked'));
    } catch (revokeError) {
      if (revokeError instanceof ApiRequestError && revokeError.status === 404) {
        void onRefresh();
      }
      message.error(getErrorMessage(revokeError, t('kgBrowser.userGraph.memoryRevokeFailed')));
    }
  };

  const columns: ColumnsType<UserGraphMemory> = [
    {
      title: t('kgBrowser.userGraph.memoryType'),
      dataIndex: 'type',
      width: 150,
      render: (type: UserGraphMemoryType) => (
        <Tag>{t(`kgBrowser.userGraph.memoryTypes.${type}`)}</Tag>
      ),
    },
    {
      title: t('kgBrowser.userGraph.description'),
      dataIndex: 'description',
      ellipsis: true,
    },
    {
      title: t('kgBrowser.userGraph.status'),
      dataIndex: 'status',
      width: 130,
      render: (status: UserGraphMemoryStatus) => (
        <Tag
          color={status === 'active' ? 'green' : status === 'needs_review' ? 'orange' : 'default'}
        >
          {t(`kgBrowser.userGraph.memoryStatuses.${status}`)}
        </Tag>
      ),
    },
    {
      title: t('kgBrowser.userGraph.updatedAt'),
      dataIndex: 'updated_at',
      width: 170,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 190,
      fixed: 'right',
      render: (_, memory) => {
        const writable = canWrite && memory.status !== 'revoked';
        const revoking = mutation?.kind === 'revoke' && mutation.memoryId === memory.id;
        return (
          <Space size={4}>
            <Button type="link" size="small" onClick={() => openDrawer('view', memory)}>
              {t('kgBrowser.userGraph.view')}
            </Button>
            {writable && (
              <Button
                type="link"
                size="small"
                disabled={Boolean(mutation)}
                onClick={() => openDrawer('edit', memory)}
              >
                {t('common.edit')}
              </Button>
            )}
            {writable && (
              <Popconfirm
                disabled={Boolean(mutation)}
                title={t('kgBrowser.userGraph.revokeConfirmTitle')}
                description={t('kgBrowser.userGraph.revokeConfirmDescription')}
                okText={t('kgBrowser.userGraph.revoke')}
                cancelText={t('common.cancel')}
                okButtonProps={{ danger: true, loading: revoking }}
                onConfirm={() => handleRevoke(memory)}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  disabled={Boolean(mutation) && !revoking}
                  loading={revoking}
                >
                  {t('kgBrowser.userGraph.revoke')}
                </Button>
              </Popconfirm>
            )}
          </Space>
        );
      },
    },
  ];

  const drawerSaving =
    mutation?.kind === 'create' ||
    (mutation?.kind === 'update' && mutation.memoryId === selectedMemory?.id);

  return (
    <>
      <Card
        title={
          <Space size={12}>
            <span>{t('kgBrowser.userGraph.memories')}</span>
            <Typography.Text type="secondary">
              {t('common.total', { count: total })}
            </Typography.Text>
          </Space>
        }
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void onRefresh()}>
              {t('common.refresh')}
            </Button>
            {canWrite && (
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={Boolean(mutation)}
                onClick={() => openDrawer('create')}
              >
                {t('kgBrowser.userGraph.createMemory')}
              </Button>
            )}
          </Space>
        }
      >
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
          <Select
            style={{ width: 180 }}
            allowClear
            value={query.type}
            placeholder={t('kgBrowser.userGraph.filterType')}
            options={MEMORY_TYPES.map((type) => ({
              value: type,
              label: t(`kgBrowser.userGraph.memoryTypes.${type}`),
            }))}
            onChange={(type) => onQueryChange({ type })}
          />
          <Select
            style={{ width: 180 }}
            allowClear
            value={query.status}
            placeholder={t('kgBrowser.userGraph.filterStatus')}
            options={MEMORY_STATUSES.map((status) => ({
              value: status,
              label: t(`kgBrowser.userGraph.memoryStatuses.${status}`),
            }))}
            onChange={(status) => onQueryChange({ status })}
          />
          <Input.Search
            style={{ flex: '1 1 260px', maxWidth: 420 }}
            allowClear
            maxLength={256}
            value={searchValue}
            placeholder={t('kgBrowser.userGraph.searchMemories')}
            onChange={(event) => {
              const nextValue = event.target.value;
              setSearchValue(nextValue);
              if (!nextValue && query.q) onQueryChange({ q: undefined });
            }}
            onSearch={(q) => onQueryChange({ q: q.trim() || undefined })}
          />
        </div>

        {error && (
          <Alert
            style={{ marginBottom: 12 }}
            type="error"
            showIcon
            message={t('kgBrowser.userGraph.memoryListFailed')}
            description={error}
            action={
              <Button size="small" onClick={() => void onRefresh()}>
                {t('common.retry')}
              </Button>
            }
          />
        )}

        <Table<UserGraphMemory>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={memories}
          pagination={false}
          scroll={{ x: 860 }}
          locale={{ emptyText: t('kgBrowser.userGraph.noMemories') }}
        />
        <Pagination
          style={{ marginTop: 16, textAlign: 'right' }}
          current={query.page}
          pageSize={query.pageSize}
          total={total}
          showSizeChanger
          pageSizeOptions={[10, 20, 50, 100]}
          onChange={onPageChange}
        />
      </Card>

      <MemoryEditorDrawer
        open={drawerOpen}
        mode={drawerMode}
        memory={selectedMemory}
        saving={Boolean(drawerSaving)}
        onClose={() => setDrawerOpen(false)}
        onLoad={onLoad}
        onCreate={onCreate}
        onUpdate={onUpdate}
        onRefresh={onRefresh}
      />
    </>
  );
}
