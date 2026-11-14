import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button,
  Empty,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import { approveAccessRequest, authFetch, listAccessRequests, rejectAccessRequest } from '@/auth';
import type { AccessRequest, AccessRequestStatus, Workspace } from '@/auth';
import { getErrorMessage } from '@/services/apiError';
import { formatDateTime } from '@/utils';

interface Props {
  /** Lets the console badge the tab with the pending count. */
  onPendingCountChange?: (count: number) => void;
}

const STATUS_TABS: AccessRequestStatus[] = ['pending', 'approved', 'rejected'];

const STATUS_COLOR: Record<AccessRequestStatus, string> = {
  pending: 'processing',
  approved: 'success',
  rejected: 'error',
  canceled: 'default',
};

const AccessRequestsTab: React.FC<Props> = ({ onPendingCountChange }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth.accessRequests' });
  const { t: tAuth } = useTranslation('translation', { keyPrefix: 'auth' });
  const [status, setStatus] = useState<AccessRequestStatus>('pending');
  const [rows, setRows] = useState<AccessRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState('');
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listAccessRequests(status);
      setRows(list);
      if (status === 'pending') onPendingCountChange?.(list.length);
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('loadFailed')));
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [status, t, onPendingCountChange]);

  useEffect(() => {
    void load();
  }, [load]);

  // Only needed to pick a landing workspace for tenant-wide requests.
  useEffect(() => {
    void authFetch<{ workspaces: Workspace[] }>('/api/workspaces')
      .then((result) => setWorkspaces((result.workspaces || []).filter((ws) => ws.can_manage)))
      .catch(() => setWorkspaces([]));
  }, []);

  const manageableActive = useMemo(
    () => workspaces.filter((ws) => ws.status === 'active'),
    [workspaces],
  );

  const runApprove = async (record: AccessRequest, workspaceId?: string) => {
    setActing(record.id);
    try {
      await approveAccessRequest(record.id, workspaceId);
      message.success(t('approveSuccess'));
      await load();
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('approveFailed')));
    } finally {
      setActing('');
    }
  };

  const handleApprove = (record: AccessRequest) => {
    if (record.target_type === 'workspace') {
      Modal.confirm({
        title: t('confirmApproveTitle'),
        content: t('confirmApproveContent', {
          name: record.name || record.user_id,
          target: record.target_name,
        }),
        okText: tAuth('common.confirm'),
        cancelText: tAuth('common.cancel'),
        onOk: () => runApprove(record),
      });
      return;
    }
    // A tenant-wide request does not say which workspace the applicant lands in.
    let picked = manageableActive[0]?.id;
    Modal.confirm({
      title: t('confirmApproveTitle'),
      okText: tAuth('common.confirm'),
      cancelText: tAuth('common.cancel'),
      content: (
        <div>
          <p>
            {t('confirmApproveContent', {
              name: record.name || record.user_id,
              target: record.target_name,
            })}
          </p>
          <Select
            style={{ width: '100%' }}
            defaultValue={picked}
            onChange={(value: string) => {
              picked = value;
            }}
            placeholder={t('pickLandingWorkspace')}
            options={manageableActive.map((ws) => ({ value: ws.id, label: ws.name }))}
          />
        </div>
      ),
      onOk: () => {
        if (!picked) {
          message.error(t('pickLandingWorkspace'));
          return Promise.reject(new Error('no workspace picked'));
        }
        return runApprove(record, picked);
      },
    });
  };

  const handleReject = (record: AccessRequest) => {
    let comment = '';
    Modal.confirm({
      title: t('confirmRejectTitle'),
      okText: tAuth('common.confirm'),
      cancelText: tAuth('common.cancel'),
      okButtonProps: { danger: true },
      content: (
        <Input.TextArea
          rows={3}
          maxLength={500}
          placeholder={t('rejectCommentPlaceholder')}
          onChange={(event) => {
            comment = event.target.value;
          }}
        />
      ),
      onOk: async () => {
        setActing(record.id);
        try {
          await rejectAccessRequest(record.id, comment);
          message.success(t('rejectSuccess'));
          await load();
        } catch (err: unknown) {
          message.error(getErrorMessage(err, t('rejectFailed')));
        } finally {
          setActing('');
        }
      },
    });
  };

  const columns: ColumnsType<AccessRequest> = [
    {
      title: t('applicant'),
      dataIndex: 'user_id',
      width: 200,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <span>{record.name || record.user_id}</span>
          <span style={{ color: '#999', fontSize: 12 }}>{record.user_id}</span>
        </Space>
      ),
    },
    {
      title: t('target'),
      dataIndex: 'target_name',
      width: 220,
      render: (_, record) => (
        <Space size={4} wrap>
          <span>{record.target_name}</span>
          <Tag>{record.target_type === 'tenant' ? t('targetTenant') : t('targetWorkspace')}</Tag>
        </Space>
      ),
    },
    {
      title: t('reason'),
      dataIndex: 'message',
      ellipsis: true,
      render: (_, record) => record.message || '-',
    },
    {
      title: tAuth('fields.status'),
      dataIndex: 'status',
      width: 120,
      render: (_, record) => (
        <Tag color={STATUS_COLOR[record.status]}>{t(`status.${record.status}`)}</Tag>
      ),
    },
    {
      title: t('submittedAt'),
      dataIndex: 'created_at',
      width: 180,
      render: (_, record) => formatDateTime(record.created_at),
    },
    {
      title: tAuth('common.actions'),
      width: 160,
      fixed: 'right',
      render: (_, record) =>
        record.status === 'pending' ? (
          <Space>
            <Button
              type="link"
              size="small"
              style={{ padding: 0 }}
              loading={acting === record.id}
              onClick={() => handleApprove(record)}
            >
              {t('approve')}
            </Button>
            <Button
              type="link"
              size="small"
              danger
              style={{ padding: 0 }}
              loading={acting === record.id}
              onClick={() => handleReject(record)}
            >
              {t('reject')}
            </Button>
          </Space>
        ) : (
          record.review_comment || '-'
        ),
    },
  ];

  return (
    <div>
      <Segmented
        value={status}
        onChange={(value) => setStatus(value as AccessRequestStatus)}
        options={STATUS_TABS.map((key) => ({ value: key, label: t(`status.${key}`) }))}
        style={{ marginBottom: 16 }}
      />
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : rows.length ? (
        <Table<AccessRequest>
          rowKey="id"
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1000 }}
          pagination={{ defaultPageSize: 10, showSizeChanger: true }}
        />
      ) : (
        <Empty description={t('empty')} />
      )}
    </div>
  );
};

export default AccessRequestsTab;
