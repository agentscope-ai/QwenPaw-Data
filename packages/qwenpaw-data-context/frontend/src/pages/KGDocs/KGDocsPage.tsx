import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Key } from 'react';
import { useHasCapability } from '@/store/authStore';
import { Button, ConfigProvider, Modal, Table, type TableProps } from '@/design';
import {
  CheckCircleFilled,
  DownloadOutlined,
  ExclamationCircleFilled,
  FileMarkdownFilled,
  FilePdfFilled,
  FileTextFilled,
  FileWordFilled,
  LoadingOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
  DeleteRowOutlined,
} from '@/design';
import { Space, Tag, Typography, Upload } from '@/design';
import type { UploadProps } from '@/design';
import { Input, Segmented } from 'antd';
import { useTranslation } from 'react-i18next';
import { kgDocsApi, type IngestStatus, type KgDocument } from '@/services/kgDocs';
import { useAppMessage } from '@/hooks/useAppMessage';
import { iosMotionTheme } from '../GraphCenter/components/iosMotion';
import DocPreviewPanel from './DocPreviewPanel';
import styles from './KGDocsPage.module.less';

const SUPPORTED_EXTENSIONS = new Set(['txt', 'docx', 'pdf', 'md']);
const SUPPORTED_FILE_ACCEPT = '.txt,.docx,.pdf,.md';
const MAX_FILE_SIZE = 50 * 1024 * 1024;
const STATUS_POLL_INTERVAL_MS = 3_000;
const SEARCH_DEBOUNCE_MS = 300;

type StatusFilter = 'all' | IngestStatus;

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '-';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${
    value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)
  } ${units[unitIndex]}`;
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function getFileExtension(filename: string): string {
  const ext = filename.split('.').pop();
  return ext ? ext.toLowerCase() : '';
}

const FILE_ICON_BY_EXTENSION: Record<string, { icon: typeof FileTextFilled; color: string }> = {
  pdf: { icon: FilePdfFilled, color: '#ff4d4f' },
  doc: { icon: FileWordFilled, color: '#2b579a' },
  docx: { icon: FileWordFilled, color: '#2b579a' },
  md: { icon: FileMarkdownFilled, color: '#f14e83' },
  txt: { icon: FileTextFilled, color: '#8c8c8c' },
};

function FileIcon({ filename }: { filename: string }) {
  const spec = FILE_ICON_BY_EXTENSION[getFileExtension(filename)] ?? FILE_ICON_BY_EXTENSION.txt;
  const Icon = spec.icon;
  return <Icon className={styles.fileIcon} style={{ color: spec.color }} />;
}

function saveBlob(blob: Blob, filename: string) {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

function resolveKgDocsError(t: ReturnType<typeof useTranslation>['t'], error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || '');
  if (raw.includes('doc_already_exists') || raw.includes('40901')) {
    return t('kgDocs.errors.nameConflict');
  }
  if (raw.includes('unsupported_file_type') || raw.includes('40002')) {
    return t('kgDocs.errors.unsupportedType');
  }
  if (raw.includes('file_too_large') || raw.includes('40003')) {
    return t('kgDocs.errors.fileTooLarge');
  }
  if (raw.includes('doc_not_found') || raw.includes('40401')) {
    return t('kgDocs.errors.notFound');
  }
  if (raw.includes('doc_build_in_progress') || raw.includes('40902')) {
    return t('kgDocs.errors.buildInProgress');
  }
  if (raw.includes('server_error') || raw.includes('50001')) {
    return t('kgDocs.errors.serverError');
  }
  return raw || t('kgDocs.errors.requestFailed');
}

export default function KGDocsPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  // KG documents are user-owned; every tenant role may manage only its own files.
  const canWrite = useHasCapability('tg_kg:self');
  const [docs, setDocs] = useState<KgDocument[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [statusCounts, setStatusCounts] = useState<Record<IngestStatus, number> | null>(null);
  const [keyword, setKeyword] = useState('');
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [previewDoc, setPreviewDoc] = useState<KgDocument | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [reingestingId, setReingestingId] = useState<string | null>(null);
  const buildingIdsRef = useRef<Set<string>>(new Set());
  const toastedFailedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedKeyword(keyword.trim());
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  useEffect(() => {
    setPage(1);
  }, [statusFilter, debouncedKeyword]);

  const loadDocs = useCallback(
    async (options?: { silent?: boolean }) => {
      const silent = options?.silent ?? false;
      if (!silent) setLoading(true);
      try {
        const data = await kgDocsApi.listKgDocs({
          page,
          pageSize,
          status: statusFilter === 'all' ? undefined : statusFilter,
          keyword: debouncedKeyword || undefined,
        });
        const nextDocs = data.list ?? [];
        for (const doc of nextDocs) {
          if (doc.ingest_status === 'building') {
            buildingIdsRef.current.add(doc.doc_id);
          } else if (
            doc.ingest_status === 'failed' &&
            buildingIdsRef.current.has(doc.doc_id) &&
            !toastedFailedRef.current.has(doc.doc_id)
          ) {
            toastedFailedRef.current.add(doc.doc_id);
            buildingIdsRef.current.delete(doc.doc_id);
            message.error(
              t(doc.retry_action === 'delete' ? 'kgDocs.deleteFailToast' : 'kgDocs.failToast', {
                name: doc.filename,
                msg: doc.ingest_error || t('kgDocs.failReasonUnknown'),
              }),
            );
          }
        }
        setDocs(nextDocs);
        setPage(data.page || page);
        setPageSize(data.page_size || pageSize);
        setTotal(data.total || 0);
        if (data.status_counts) setStatusCounts(data.status_counts);
      } catch (error) {
        console.error('Failed to load KG docs:', error);
        if (!silent) {
          message.error(resolveKgDocsError(t, error));
          setDocs([]);
          setTotal(0);
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [debouncedKeyword, message, page, pageSize, statusFilter, t],
  );

  useEffect(() => {
    void loadDocs();
  }, [loadDocs]);

  useEffect(() => {
    if (!docs.some((doc) => doc.ingest_status === 'building')) return;

    const timer = window.setTimeout(() => {
      void loadDocs({ silent: true });
    }, STATUS_POLL_INTERVAL_MS);

    return () => window.clearTimeout(timer);
  }, [docs, loadDocs]);

  const validateFile = useCallback(
    (file: File) => {
      const extension = getFileExtension(file.name);
      if (!SUPPORTED_EXTENSIONS.has(extension)) {
        message.error(t('kgDocs.errors.unsupportedType'));
        return false;
      }
      if (file.size > MAX_FILE_SIZE) {
        message.error(t('kgDocs.errors.fileTooLarge'));
        return false;
      }
      return true;
    },
    [message, t],
  );

  const uploadDocument = useCallback(
    async (file: File) => {
      if (!validateFile(file)) return;
      setUploading(true);
      try {
        const uploaded = await kgDocsApi.uploadKgDoc(file);
        message.success(t('kgDocs.uploadSuccess', { name: uploaded.filename }));
        if (page === 1) {
          await loadDocs();
        } else {
          setPage(1);
        }
      } catch (error) {
        console.error('Failed to upload KG doc:', error);
        message.error(resolveKgDocsError(t, error));
      } finally {
        setUploading(false);
      }
    },
    [loadDocs, message, page, t, validateFile],
  );

  const beforeUpload = useCallback<NonNullable<UploadProps['beforeUpload']>>(
    (file, fileList) => {
      if (fileList.length > 1) {
        if (file === fileList[0]) {
          message.error(t('kgDocs.errors.singleFileOnly'));
        }
        return Upload.LIST_IGNORE;
      }
      if (uploading) return Upload.LIST_IGNORE;
      void uploadDocument(file as File);
      return Upload.LIST_IGNORE;
    },
    [message, t, uploadDocument, uploading],
  );

  const handleDownload = useCallback(
    async (record: KgDocument) => {
      setDownloadingId(record.doc_id);
      try {
        const blob = await kgDocsApi.downloadKgDocBlob(record.doc_id);
        saveBlob(blob, record.filename);
      } catch (error) {
        console.error('Failed to download KG doc:', error);
        message.error(resolveKgDocsError(t, error));
      } finally {
        setDownloadingId((current) => (current === record.doc_id ? null : current));
      }
    },
    [message, t],
  );

  const handleReingest = useCallback(
    async (record: KgDocument) => {
      setReingestingId(record.doc_id);
      try {
        await kgDocsApi.reingestKgDoc(record.doc_id);
        // 允许本轮构建若再失败时重新弹 toast。
        toastedFailedRef.current.delete(record.doc_id);
        message.success(t('kgDocs.reingestStarted', { name: record.filename }));
        // 状态回到 building 后，轮询 effect 会自动接管刷新。
        await loadDocs({ silent: true });
      } catch (error) {
        console.error('Failed to reingest KG doc:', error);
        message.error(resolveKgDocsError(t, error));
      } finally {
        setReingestingId((current) => (current === record.doc_id ? null : current));
      }
    },
    [loadDocs, message, t],
  );

  const handleDelete = useCallback(
    (record: KgDocument) => {
      Modal.confirm({
        title: t('kgDocs.confirmDeleteTitle'),
        content: t('kgDocs.confirmDelete', { name: record.filename }),
        okText: t('common.delete'),
        okType: 'primary',
        cancelText: t('common.cancel'),
        onOk: async () => {
          try {
            await kgDocsApi.deleteKgDoc(record.doc_id);
            message.success(t('kgDocs.deleteSuccess'));
            if (previewDoc?.doc_id === record.doc_id) {
              setPreviewDoc(null);
            }
            if (docs.length === 1 && page > 1) {
              setPage(page - 1);
            } else {
              await loadDocs();
            }
          } catch (error) {
            console.error('Failed to delete KG doc:', error);
            message.error(resolveKgDocsError(t, error));
            await loadDocs({ silent: true });
          }
        },
      });
    },
    [docs.length, loadDocs, message, page, previewDoc?.doc_id, t],
  );

  const deleteSelected = useCallback(() => {
    Modal.confirm({
      title: t('kgDocs.confirmDeleteTitle'),
      content: t('kgDocs.confirmDeleteSelected', { count: selectedRowKeys.length }),
      okText: t('common.delete'),
      okType: 'primary',
      cancelText: t('common.cancel'),
      onOk: async () => {
        setLoading(true);
        let deletedCount = 0;
        console.log('selectedRowKeys:' + JSON.stringify(selectedRowKeys));
        for (const docId of selectedRowKeys) {
          try {
            await kgDocsApi.deleteKgDoc(String(docId));
            if (previewDoc?.doc_id === docId) {
              setPreviewDoc(null);
            }
            deletedCount += 1;
          } catch (error) {
            console.error('Failed to delete KG doc:' + docId, error);
            message.error(resolveKgDocsError(t, error));
          }
        }

        try {
          if (docs.length === deletedCount && page > 1) {
            setPage(page - 1);
          } else {
            await loadDocs();
          }
        } catch (error) {
          console.error('Failed to reload KG doc page:', error);
        }
        setSelectedRowKeys([]);
        message.success(t('kgDocs.deleteSuccess'));
        setLoading(false);
      },
    });
  }, [selectedRowKeys.length, docs.length, loadDocs, message, page, previewDoc?.doc_id, t]);

  const statusTabs = useMemo(
    () => [
      { label: t('kgDocs.tabs.all'), value: 'all' as StatusFilter },
      {
        label: `${t('kgDocs.status.building')} ${statusCounts?.building ?? 0}`,
        value: 'building' as StatusFilter,
      },
      {
        label: `${t('kgDocs.status.ready')} ${statusCounts?.ready ?? 0}`,
        value: 'ready' as StatusFilter,
      },
      {
        label: `${t('kgDocs.status.failed')} ${statusCounts?.failed ?? 0}`,
        value: 'failed' as StatusFilter,
      },
    ],
    [statusCounts, t],
  );

  const columns: TableProps<KgDocument>['columns'] = useMemo(
    () => [
      {
        title: t('kgDocs.filename'),
        dataIndex: 'filename',
        key: 'filename',
        width: 300,
        ellipsis: { showTitle: false },
        render: (filename: string, record) => (
          <button
            type="button"
            className={styles.filenameButton}
            onClick={() => setPreviewDoc(record)}
          >
            <span className={styles.filenameCell}>
              <FileIcon filename={filename} />
              <Typography.Text className={styles.filename} ellipsis>
                {filename}
              </Typography.Text>
            </span>
          </button>
        ),
      },
      {
        title: t('kgDocs.fileType'),
        dataIndex: 'filename',
        key: 'file_type',
        width: 100,
        render: (filename: string) => getFileExtension(filename) || '-',
      },
      {
        title: t('kgDocs.fileSize'),
        dataIndex: 'file_size',
        key: 'file_size',
        width: 110,
        render: (size: number) => formatFileSize(size),
      },
      {
        title: t('kgDocs.owner'),
        dataIndex: 'owner_name',
        key: 'owner_name',
        width: 150,
        ellipsis: true,
        render: (ownerName: string, record) => ownerName || record.owner_id,
      },
      {
        title: t('kgDocs.kgStatus'),
        dataIndex: 'ingest_status',
        key: 'ingest_status',
        width: 140,
        render: (status: KgDocument['ingest_status'] | undefined, record) => {
          if (status === 'building') {
            return (
              <Tag color="processing" icon={<LoadingOutlined spin />} className={styles.statusTag}>
                {t('kgDocs.status.building')}
              </Tag>
            );
          }
          if (status === 'failed') {
            return (
              <Tag color="error" icon={<ExclamationCircleFilled />} className={styles.statusTag}>
                {t(
                  record.retry_action === 'delete'
                    ? 'kgDocs.status.deleteFailed'
                    : 'kgDocs.status.failed',
                )}
              </Tag>
            );
          }
          return (
            <Tag color="success" icon={<CheckCircleFilled />} className={styles.statusTag}>
              {t('kgDocs.status.ready')}
            </Tag>
          );
        },
      },
      {
        title: t('kgDocs.failReasonCol'),
        dataIndex: 'ingest_error',
        key: 'ingest_error',
        width: 360,
        render: (error: string | null, record) => {
          if (record.ingest_status !== 'failed') return '—';
          const reason = error || t('kgDocs.failReasonUnknown');
          return (
            <Typography.Text type="danger" className={styles.failReason}>
              {reason}
            </Typography.Text>
          );
        },
      },
      {
        title: t('kgDocs.updatedAt'),
        dataIndex: 'last_modified',
        key: 'last_modified',
        width: 190,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t('common.actions'),
        key: 'actions',
        width: 200,
        render: (_, record) => (
          <Space size={16}>
            <Button
              type="link"
              icon={<DownloadOutlined />}
              className={styles.actionLink}
              disabled={!record.doc_id || downloadingId === record.doc_id}
              loading={downloadingId === record.doc_id}
              onClick={(event) => {
                event.stopPropagation();
                void handleDownload(record);
              }}
            >
              {t('common.download')}
            </Button>
            {canWrite && record.ingest_status === 'failed' && record.retry_action && (
              <Button
                type="link"
                icon={<ReloadOutlined />}
                className={styles.actionLink}
                disabled={!record.doc_id || reingestingId === record.doc_id}
                loading={reingestingId === record.doc_id}
                onClick={(event) => {
                  event.stopPropagation();
                  if (record.retry_action === 'delete') {
                    handleDelete(record);
                  } else {
                    void handleReingest(record);
                  }
                }}
              >
                {t(record.retry_action === 'delete' ? 'kgDocs.retryDelete' : 'kgDocs.retry')}
              </Button>
            )}
            {canWrite && record.can_delete && (
              <Button
                type="link"
                className={styles.actionLink}
                onClick={(event) => {
                  event.stopPropagation();
                  handleDelete(record);
                }}
              >
                {t('common.delete')}
              </Button>
            )}
          </Space>
        ),
      },
    ],
    [canWrite, downloadingId, handleDelete, handleDownload, handleReingest, reingestingId, t],
  );

  return (
    <ConfigProvider theme={iosMotionTheme}>
      <div className={styles.kgDocsPage}>
        <div className={styles.workspace}>
          <div className={styles.contentPanel}>
            <div className={styles.header}>
              <div className={styles.headerLeft}>
                <span className={styles.tableTotal}>{t('common.total', { count: total })}</span>
                <Segmented<StatusFilter>
                  options={statusTabs}
                  value={statusFilter}
                  onChange={(value) => setStatusFilter(value)}
                />
                <Button
                  type="primary"
                  icon={<DeleteRowOutlined />}
                  onClick={() => void deleteSelected()}
                  loading={loading}
                  disabled={selectedRowKeys === undefined || selectedRowKeys.length < 1}
                >
                  {t('kgDocs.deleteSelected')}
                </Button>
              </div>
              <div className={styles.headerRight}>
                <Input
                  allowClear
                  className={styles.searchInput}
                  placeholder={t('kgDocs.searchPlaceholder')}
                  prefix={<SearchOutlined />}
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                />
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => void loadDocs()}
                  loading={loading}
                  aria-label={t('common.refresh')}
                />
                {canWrite && (
                  <Upload
                    className={styles.inlineUpload}
                    accept={SUPPORTED_FILE_ACCEPT}
                    beforeUpload={beforeUpload}
                    disabled={uploading}
                    maxCount={1}
                    multiple={false}
                    showUploadList={false}
                  >
                    <Button type="primary" icon={<UploadOutlined />} loading={uploading}>
                      {t('kgDocs.upload')}
                    </Button>
                  </Upload>
                )}
              </div>
            </div>

            <div className={`${styles.tablePanel} ${loading ? styles.tableRefreshing : ''}`}>
              <Table<KgDocument>
                columns={columns}
                dataSource={docs}
                loading={loading}
                rowKey="doc_id"
                rowClassName={(record) =>
                  record.doc_id === previewDoc?.doc_id ? styles.selectedRow : ''
                }
                rowSelection={{
                  columnWidth: 48,
                  selectedRowKeys,
                  onChange: (keys) => setSelectedRowKeys(keys),
                }}
                scroll={{ x: previewDoc ? 860 : 1_100 }}
                pagination={{
                  current: page,
                  pageSize,
                  total,
                  showSizeChanger: true,
                  pageSizeOptions: [10, 20, 50, 100],
                  onChange: (nextPage, nextPageSize) => {
                    setPage(nextPage);
                    setPageSize(nextPageSize);
                  },
                }}
              />
            </div>
          </div>
          <DocPreviewPanel doc={previewDoc} onClose={() => setPreviewDoc(null)} />
        </div>
      </div>
    </ConfigProvider>
  );
}
