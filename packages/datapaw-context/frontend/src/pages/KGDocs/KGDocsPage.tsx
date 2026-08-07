import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Modal, Table, type TableProps } from "@/design";
import {
  CheckCircleFilled,
  DeleteOutlined,
  DownloadOutlined,
  ExclamationCircleFilled,
  LoadingOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@/design";
import { Space, Tag, Tooltip, Typography, Upload } from "@/design";
import type { UploadProps } from "@/design";
import { useTranslation } from "react-i18next";
import { kgDocsApi, type KgDocument } from "@/services/kgDocs";
import { useAppMessage } from "@/hooks/useAppMessage";
import styles from "./KGDocsPage.module.less";

const SUPPORTED_EXTENSIONS = new Set(["txt", "docx", "pdf", "md"]);
const SUPPORTED_FILE_ACCEPT = ".txt,.docx,.pdf,.md";
const MAX_FILE_SIZE = 50 * 1024 * 1024;
const STATUS_POLL_INTERVAL_MS = 3_000;

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
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

function getFileExtension(filename: string): string {
  const ext = filename.split(".").pop();
  return ext ? ext.toLowerCase() : "";
}

function resolveKgDocsError(
  t: ReturnType<typeof useTranslation>["t"],
  error: unknown,
): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (raw.includes("doc_already_exists") || raw.includes("40901")) {
    return t("kgDocs.errors.nameConflict");
  }
  if (raw.includes("unsupported_file_type") || raw.includes("40002")) {
    return t("kgDocs.errors.unsupportedType");
  }
  if (raw.includes("file_too_large") || raw.includes("40003")) {
    return t("kgDocs.errors.fileTooLarge");
  }
  if (raw.includes("doc_not_found") || raw.includes("40401")) {
    return t("kgDocs.errors.notFound");
  }
  if (raw.includes("server_error") || raw.includes("50001")) {
    return t("kgDocs.errors.serverError");
  }
  return raw || t("kgDocs.errors.requestFailed");
}

export default function KGDocsPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [docs, setDocs] = useState<KgDocument[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);

  const loadDocs = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;
    if (!silent) setLoading(true);
    try {
      const data = await kgDocsApi.listKgDocs({ page, pageSize });
      setDocs(data.list ?? []);
      setPage(data.page || page);
      setPageSize(data.page_size || pageSize);
      setTotal(data.total || 0);
    } catch (error) {
      console.error("Failed to load KG docs:", error);
      if (!silent) {
        message.error(resolveKgDocsError(t, error));
        setDocs([]);
        setTotal(0);
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [message, page, pageSize, t]);

  useEffect(() => {
    void loadDocs();
  }, [loadDocs]);

  useEffect(() => {
    if (!docs.some((doc) => doc.ingest_status === "building")) return;

    const timer = window.setTimeout(() => {
      void loadDocs({ silent: true });
    }, STATUS_POLL_INTERVAL_MS);

    return () => window.clearTimeout(timer);
  }, [docs, loadDocs]);

  const validateFile = useCallback(
    (file: File) => {
      const extension = getFileExtension(file.name);
      if (!SUPPORTED_EXTENSIONS.has(extension)) {
        message.error(t("kgDocs.errors.unsupportedType"));
        return false;
      }
      if (file.size > MAX_FILE_SIZE) {
        message.error(t("kgDocs.errors.fileTooLarge"));
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
        message.success(t("kgDocs.uploadSuccess", { name: uploaded.filename }));
        if (page === 1) {
          await loadDocs();
        } else {
          setPage(1);
        }
      } catch (error) {
        console.error("Failed to upload KG doc:", error);
        message.error(resolveKgDocsError(t, error));
      } finally {
        setUploading(false);
      }
    },
    [loadDocs, message, page, t, validateFile],
  );

  const beforeUpload = useCallback<NonNullable<UploadProps["beforeUpload"]>>(
    (file, fileList) => {
      if (fileList.length > 1) {
        if (file === fileList[0]) {
          message.error(t("kgDocs.errors.singleFileOnly"));
        }
        return Upload.LIST_IGNORE;
      }
      if (uploading) return Upload.LIST_IGNORE;
      void uploadDocument(file as File);
      return Upload.LIST_IGNORE;
    },
    [message, t, uploadDocument, uploading],
  );

  const handleDelete = useCallback(
    (record: KgDocument) => {
      Modal.confirm({
        title: t("kgDocs.confirmDeleteTitle"),
        content: t("kgDocs.confirmDelete", { name: record.filename }),
        okText: t("common.delete"),
        okType: "primary",
        cancelText: t("common.cancel"),
        onOk: async () => {
          try {
            await kgDocsApi.deleteKgDoc(record.doc_id);
            message.success(t("kgDocs.deleteSuccess"));
            if (docs.length === 1 && page > 1) {
              setPage(page - 1);
            } else {
              await loadDocs();
            }
          } catch (error) {
            console.error("Failed to delete KG doc:", error);
            message.error(resolveKgDocsError(t, error));
          }
        },
      });
    },
    [docs.length, loadDocs, message, page, t],
  );

  const columns: TableProps<KgDocument>["columns"] = useMemo(
    () => [
      {
        title: t("kgDocs.filename"),
        dataIndex: "filename",
        key: "filename",
        width: 260,
        ellipsis: { showTitle: false },
        render: (filename: string) => (
          <Typography.Text className={styles.filename} ellipsis>
            {filename}
          </Typography.Text>
        ),
      },
      {
        title: t("kgDocs.fileSize"),
        dataIndex: "file_size",
        key: "file_size",
        width: 140,
        render: (size: number) => formatFileSize(size),
      },
      {
        title: t("kgDocs.kgStatus"),
        dataIndex: "ingest_status",
        key: "ingest_status",
        width: 160,
        render: (status: KgDocument["ingest_status"] | undefined, record) => {
          if (status === "building") {
            return (
              <Tag
                color="processing"
                icon={<LoadingOutlined spin />}
                className={styles.statusTag}
              >
                {t("kgDocs.status.building")}
              </Tag>
            );
          }
          if (status === "failed") {
            const failedTag = (
              <Tag
                color="error"
                icon={<ExclamationCircleFilled />}
                className={styles.statusTag}
              >
                {t("kgDocs.status.failed")}
              </Tag>
            );
            if (!record.ingest_error) return failedTag;
            return (
              <Tooltip title={record.ingest_error}>
                <span
                  className={styles.statusTooltip}
                  tabIndex={0}
                  aria-label={`${t("kgDocs.status.failed")}: ${record.ingest_error}`}
                >
                  {failedTag}
                </span>
              </Tooltip>
            );
          }
          return (
            <Tag
              color="success"
              icon={<CheckCircleFilled />}
              className={styles.statusTag}
            >
              {t("kgDocs.status.ready")}
            </Tag>
          );
        },
      },
      {
        title: t("common.actions"),
        key: "actions",
        width: 190,
        render: (_, record) => (
          <Space size={4}>
            <Button
              type="link"
              icon={<DownloadOutlined />}
              disabled={!record.download_url}
              onClick={() =>
                window.open(
                  record.download_url,
                  "_blank",
                  "noopener,noreferrer",
                )
              }
            >
              {t("common.download")}
            </Button>
            <Button
              type="link"
              danger
              icon={<DeleteOutlined />}
              className={styles.deleteAction}
              onClick={() => handleDelete(record)}
            >
              {t("common.delete")}
            </Button>
          </Space>
        ),
      },
    ],
    [handleDelete, t],
  );

  return (
    <div className={styles.kgDocsPage}>
      <div className={styles.contentPanel}>
        <div className={styles.toolbar}>
          <Space size={8} wrap>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void loadDocs()}
              loading={loading}
            >
              {t("common.refresh")}
            </Button>
            <Upload
              className={styles.inlineUpload}
              accept={SUPPORTED_FILE_ACCEPT}
              beforeUpload={beforeUpload}
              disabled={uploading}
              maxCount={1}
              multiple={false}
              showUploadList={false}
            >
              <Button
                type="primary"
                icon={<UploadOutlined />}
                loading={uploading}
              >
                {t("kgDocs.upload")}
              </Button>
            </Upload>
          </Space>
        </div>

        <div className={styles.tablePanel}>
          <Table<KgDocument>
            columns={columns}
            dataSource={docs}
            loading={loading}
            rowKey="doc_id"
            scroll={{ x: 920 }}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50, 100],
              showTotal: (count) => t("common.total", { count }),
              onChange: (nextPage, nextPageSize) => {
                setPage(nextPage);
                setPageSize(nextPageSize);
              },
            }}
          />
        </div>
      </div>
    </div>
  );
}
