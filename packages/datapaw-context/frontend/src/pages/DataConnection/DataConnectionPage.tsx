import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createDataSource,
  deleteDataSource,
  queryDataSourceList,
  testDataSourceConnection,
  testSavedDataSourceConnection,
  updateDataSource,
} from "@/services/dataSource";
import type { DataSourceConfig, DataSourceItem, DataSourceType } from "@/types/dataSource";
import { isFormValidationError } from "@/utils";
import { normalizeListResponse } from "@/utils/listResponse";
import { PlusOutlined } from "@/design";
import { Button, Drawer, Form, Input, Modal, Select, Space, Table, message, type TableProps } from "@/design";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import { useDataSourceOptionsStore } from "@/store";
import { DATA_CONNECTION_TYPE_META } from "./helpers/types";
import styles from "./DataConnectionPage.module.less";

type NormalizedDataSourceType = "mysql" | "postgresql" | "odps";

interface DataConnectionFormValues {
  datasource_name: string;
  datasource_type: NormalizedDataSourceType;
  host?: string;
  port?: number | string;
  user?: string;
  password?: string;
  dbname?: string;
  database?: string;
  endpoint?: string;
  project?: string;
  access_key_id?: string;
  access_key_secret?: string;
  sts_token?: string;
}

const DATA_SOURCE_TYPE_OPTIONS = [
  { label: "MySQL", value: "mysql" },
  { label: "PostgreSQL", value: "postgresql" },
  { label: "ODPS", value: "odps" },
];

function normalizeType(type?: DataSourceType | string | null): NormalizedDataSourceType {
  const value = String(type || "").toLowerCase();
  if (value === "postgresql" || value === "odps") return value;
  return "mysql";
}

function optionalNumber(value: number | string | undefined): number | undefined {
  if (value === undefined || value === "") return undefined;
  const next = Number(value);
  return Number.isFinite(next) ? next : undefined;
}

function getConfigValue(config: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = config[key];
    if (typeof value === "string") return value;
    if (typeof value === "number") return String(value);
  }
  return undefined;
}

function configToFormValues(record: DataSourceItem): Partial<DataConnectionFormValues> {
  const config = (record.config || {}) as Record<string, unknown>;
  return {
    datasource_name: record.datasource_name,
    datasource_type: normalizeType(record.datasource_type),
    host: getConfigValue(config, "host"),
    port: getConfigValue(config, "port"),
    user: getConfigValue(config, "user"),
    password: getConfigValue(config, "password"),
    dbname: getConfigValue(config, "dbname", "db"),
    database: getConfigValue(config, "database", "db"),
    endpoint: getConfigValue(config, "endpoint"),
    project: getConfigValue(config, "project", "project_name"),
    access_key_id: getConfigValue(config, "access_key_id", "access_id"),
    access_key_secret: getConfigValue(config, "access_key_secret", "access_key"),
    sts_token: getConfigValue(config, "sts_token"),
  };
}

function valuesToPayload(values: DataConnectionFormValues): Partial<DataSourceItem> {
  const datasource_type = normalizeType(values.datasource_type);
  let config: DataSourceConfig;

  if (datasource_type === "odps") {
    config = {
      endpoint: values.endpoint?.trim() || "",
      project: values.project?.trim() || "",
      access_key_id: values.access_key_id?.trim() || "",
      access_key_secret: values.access_key_secret || "",
      sts_token: values.sts_token?.trim() || null,
    };
  } else if (datasource_type === "postgresql") {
    config = {
      host: values.host?.trim() || "",
      port: optionalNumber(values.port) ?? 5432,
      dbname: values.dbname?.trim() || "",
      user: values.user?.trim() || "",
      password: values.password || "",
    };
  } else {
    config = {
      host: values.host?.trim() || "",
      port: optionalNumber(values.port) ?? 3306,
      database: values.database?.trim() || "",
      user: values.user?.trim() || "",
      password: values.password || "",
    };
  }

  return {
    datasource_name: values.datasource_name.trim(),
    datasource_type,
    config,
  };
}

/** Data Connection list page backed by the current semantic-config data source API. */
export default function DataConnectionPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [form] = Form.useForm<DataConnectionFormValues>();
  const [connections, setConnections] = useState<DataSourceItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingRecord, setEditingRecord] = useState<DataSourceItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [testingForm, setTestingForm] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const selectedType = Form.useWatch("datasource_type", form);
  const selectedNormalizedType = useMemo(() => normalizeType(selectedType), [selectedType]);
  const isEdit = Boolean(editingRecord);

  const loadConnections = useCallback(async () => {
    setLoading(true);
    try {
      const res = await queryDataSourceList({ page: 1, size: 500 });
      setConnections(normalizeListResponse<DataSourceItem>(res));
    } catch (error) {
      console.error("Failed to load data connections:", error);
      setConnections([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConnections();
  }, [loadConnections]);

  const resetDataSourceOptionCache = () => {
    useDataSourceOptionsStore.getState().reset();
  };

  const reloadConnections = async () => {
    resetDataSourceOptionCache();
    await loadConnections();
  };

  const openAddDrawer = useCallback(() => {
    setEditingRecord(null);
    form.resetFields();
    form.setFieldsValue({
      datasource_type: "mysql",
      port: 3306,
    });
    setDrawerOpen(true);
  }, [form]);

  useEffect(() => {
    if (searchParams.get("action") !== "add") return;
    openAddDrawer();
    const next = new URLSearchParams(searchParams);
    next.delete("action");
    setSearchParams(next, { replace: true });
  }, [openAddDrawer, searchParams, setSearchParams]);

  const openEditDrawer = (record: DataSourceItem) => {
    setEditingRecord(record);
    form.resetFields();
    form.setFieldsValue(configToFormValues(record));
    setDrawerOpen(true);
  };

  const closeDrawer = () => {
    setDrawerOpen(false);
    setEditingRecord(null);
    form.resetFields();
  };

  const handleTypeChange = (type: NormalizedDataSourceType) => {
    form.setFieldsValue({
      host: undefined,
      port: type === "mysql" ? 3306 : type === "postgresql" ? 5432 : undefined,
      user: undefined,
      password: undefined,
      dbname: undefined,
      database: undefined,
      endpoint: undefined,
      project: undefined,
      access_key_id: undefined,
      access_key_secret: undefined,
      sts_token: undefined,
    });
  };

  const handleTestFormConnection = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values);
      setTestingForm(true);
      const result = await testDataSourceConnection({
        datasource_type: payload.datasource_type,
        config: payload.config,
      });
      if (result.success) {
        message.success(t("dataConnection.testSuccess", {
          message: result.message,
          latency: result.elapsed_ms,
        }));
      } else {
        message.error(result.message || t("dataSourceManagement.testFailed"));
      }
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
    } finally {
      setTestingForm(false);
    }
  };

  const handleTestSavedConnection = async (record: DataSourceItem) => {
    try {
      setTestingId(record.datasource_id);
      const result = await testSavedDataSourceConnection(record.datasource_id);
      if (result.success) {
        message.success(t("dataConnection.testSuccess", {
          message: result.message,
          latency: result.elapsed_ms,
        }));
      } else {
        message.error(result.message || t("dataSourceManagement.testFailed"));
      }
    } finally {
      setTestingId(null);
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values);
      setSubmitting(true);
      if (editingRecord) {
        await updateDataSource(editingRecord.datasource_id, payload);
        message.success(t("common.editSuccess"));
      } else {
        await createDataSource(payload);
        message.success(t("common.createSuccess"));
      }
      closeDrawer();
      await reloadConnections();
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = (record: DataSourceItem) => {
    Modal.confirm({
      title: t("dataConnection.confirmDeleteTitle"),
      content: t("dataConnection.confirmDelete", { name: record.datasource_name }),
      okText: t("common.delete"),
      okButtonProps: { danger: true },
      cancelText: t("common.cancel"),
      onOk: async () => {
        await deleteDataSource(record.datasource_id);
        message.success(t("dataConnection.deleteSuccess"));
        await reloadConnections();
      },
    });
  };

  const columns: TableProps<DataSourceItem>["columns"] = [
    {
      title: t("dataConnection.type"),
      dataIndex: "datasource_type",
      key: "datasource_type",
      width: 220,
      render: (type: DataSourceType) => {
        const normalized = normalizeType(type);
        const meta = DATA_CONNECTION_TYPE_META[normalized];
        return (
          <div className={styles.typeCell}>
            <span className={styles.typeBadge} style={{ backgroundColor: meta.accent }}>
              {meta.badge}
            </span>
            <span className={styles.typeLabel}>{t(meta.labelKey)}</span>
          </div>
        );
      },
    },
    {
      title: t("dataConnection.name"),
      dataIndex: "datasource_name",
      key: "datasource_name",
      ellipsis: true,
    },
    {
      title: <span>{t("fields.dataSourceId")}</span>,
      dataIndex: "datasource_id",
      key: "datasource_id",
      width: 220,
      ellipsis: true,
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 220,
      render: (_, record) => (
        <Space size="small">
          <Button type="link" style={{ padding: 0 }} onClick={() => openEditDrawer(record)}>
            {t("common.edit")}
          </Button>
          <Button
            type="link"
            style={{ padding: 0 }}
            loading={testingId === record.datasource_id}
            onClick={() => void handleTestSavedConnection(record)}
          >
            {t("dataConnection.testConnection")}
          </Button>
          <Button type="link" danger style={{ padding: 0 }} onClick={() => handleDelete(record)}>
            {t("common.delete")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className={styles.dataConnectionPage}>
      <div className={styles.contentPanel}>
        <div className={styles.toolbar}>
          <Button type="primary" icon={<PlusOutlined />} onClick={openAddDrawer}>
            {t("dataConnection.add")}
          </Button>
        </div>

        <div className={styles.tablePanel}>
          <Table<DataSourceItem>
            columns={columns}
            dataSource={connections}
            loading={loading}
            rowKey="datasource_id"
            pagination={{
              pageSize: 10,
              showTotal: (total) => t("common.total", { count: total }),
            }}
          />
        </div>
      </div>

      <Drawer
        title={isEdit ? t("dataSourceManagement.editTitle") : t("dataConnection.addModalTitle")}
        placement="right"
        open={drawerOpen}
        onClose={closeDrawer}
        destroyOnHidden
        width={720}
        footer={(
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <Button loading={testingForm} onClick={() => void handleTestFormConnection()}>
              {t("dataConnection.testConnection")}
            </Button>
            <Space>
              <Button onClick={closeDrawer}>{t("common.cancel")}</Button>
              <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                {t("common.confirm")}
              </Button>
            </Space>
          </div>
        )}
      >
        <Form form={form} layout="vertical">
          {isEdit ? (
            <Form.Item label={t("fields.dataSourceId")}>
              <Input value={editingRecord?.datasource_id || ""} disabled />
            </Form.Item>
          ) : null}

          <Form.Item
            name="datasource_name"
            label={t("dataConnection.name")}
            rules={[{ required: true, message: t("dataConnection.nameRequired") }]}
          >
            <Input placeholder={t("dataConnection.namePlaceholder")} allowClear />
          </Form.Item>

          <Form.Item
            name="datasource_type"
            label={t("dataConnection.type")}
            rules={[{ required: true, message: t("dataConnection.typeRequired") }]}
          >
            <Select
              placeholder={t("dataConnection.typePlaceholder")}
              options={DATA_SOURCE_TYPE_OPTIONS}
              onChange={handleTypeChange}
            />
          </Form.Item>

          {selectedNormalizedType === "odps" ? (
            <>
              <Form.Item
                name="endpoint"
                label={t("dataConnection.endpoint")}
                rules={[{ required: true, message: t("dataConnection.endpointRequired") }]}
              >
                <Input placeholder={t("dataConnection.endpointPlaceholder")} allowClear />
              </Form.Item>
              <Form.Item
                name="project"
                label={t("dataConnection.projectName")}
                rules={[{ required: true, message: t("dataConnection.projectNameRequired") }]}
              >
                <Input placeholder={t("dataConnection.projectNamePlaceholder")} allowClear />
              </Form.Item>
              <Form.Item
                name="access_key_id"
                label={t("dataConnection.accessId")}
                rules={[{ required: true, message: t("dataConnection.accessIdRequired") }]}
              >
                <Input placeholder={t("dataConnection.accessIdPlaceholder")} allowClear />
              </Form.Item>
              <Form.Item
                name="access_key_secret"
                label={t("dataConnection.accessKey")}
                rules={[{ required: !isEdit, message: t("dataConnection.accessKeyRequired") }]}
              >
                <Input.Password
                  placeholder={isEdit ? "留空以保留现有凭证" : t("dataConnection.accessKeyPlaceholder")}
                />
              </Form.Item>
              <Form.Item name="sts_token" label="STS Token">
                <Input placeholder="STS Token" allowClear />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item
                name="host"
                label={t("dataConnection.host")}
                rules={[{ required: true, message: t("dataConnection.hostRequired") }]}
              >
                <Input placeholder={t("dataConnection.hostPlaceholder")} allowClear />
              </Form.Item>
              <Form.Item
                name="port"
                label={t("dataConnection.port")}
                rules={[{ required: true, message: t("dataConnection.portRequired") }]}
              >
                <Input type="number" placeholder={t("dataConnection.portPlaceholder")} />
              </Form.Item>
              <Form.Item
                name="user"
                label={t("dataConnection.user")}
                rules={[{ required: true, message: t("dataConnection.userRequired") }]}
              >
                <Input placeholder={t("dataConnection.userPlaceholder")} allowClear />
              </Form.Item>
              <Form.Item
                name="password"
                label={t("dataConnection.password")}
                rules={[{ required: !isEdit, message: t("dataConnection.passwordRequired") }]}
              >
                <Input.Password
                  placeholder={isEdit ? "留空以保留现有密码" : t("dataConnection.passwordPlaceholder")}
                />
              </Form.Item>
              <Form.Item
                name={selectedNormalizedType === "postgresql" ? "dbname" : "database"}
                label={t("dataConnection.db")}
                rules={[{ required: true, message: t("dataConnection.dbRequired") }]}
              >
                <Input placeholder={t("dataConnection.dbPlaceholder")} allowClear />
              </Form.Item>
            </>
          )}
        </Form>
      </Drawer>
    </div>
  );
}
