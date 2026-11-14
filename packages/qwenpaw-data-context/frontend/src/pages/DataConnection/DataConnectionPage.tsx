import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  createDataSource,
  deleteDataSource,
  queryDataSourceList,
  testDataSourceConnection,
  testSavedDataSourceConnection,
  updateDataSource,
} from '@/services/dataSource';
import type {
  DataSourceConfig,
  DataSourceCreateRequest,
  DataSourceItem,
  DataSourceType,
} from '@/types/dataSource';
import { isFormValidationError } from '@/utils';
import { PlusOutlined } from '@/design';
import { Button, Drawer, Form, Input, Select, Space, Table, type TableProps } from '@/design';
import { Alert } from 'antd';
import { useAppMessage } from '@/hooks/useAppMessage';
import { unwrapConnectionTestResult } from '@/utils/connectionTest';
import type { DataSourceConnectionTestResult } from '@/types/dataSource';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { useDataSourceOptionsStore } from '@/store';
import { useHasCapability } from '@/store/authStore';
import { DATA_CONNECTION_TYPE_META, type DataConnectionType } from './helpers/types';
import styles from './DataConnectionPage.module.less';

type NormalizedDataSourceType = DataConnectionType;

interface DataConnectionFormValues {
  datasource_name: string;
  datasource_type: NormalizedDataSourceType;
  description?: string;
  host?: string;
  port?: number | string;
  user?: string;
  password?: string;
  database?: string;
  endpoint?: string;
  project?: string;
  access_key_id?: string;
  access_key_secret?: string;
  sts_token?: string;
  path?: string;
  credentials_json?: string;
  location?: string;
  maximum_bytes_billed?: number | string;
}

const DATA_SOURCE_TYPE_OPTIONS = [
  { label: 'MySQL', value: 'mysql' },
  { label: 'PostgreSQL', value: 'postgresql' },
  { label: 'ODPS', value: 'odps' },
  { label: 'StarRocks', value: 'starrocks' },
  { label: 'Apache Doris', value: 'doris' },
  { label: 'TiDB', value: 'tidb' },
  { label: 'DuckDB', value: 'duckdb' },
  { label: 'SQLite', value: 'sqlite' },
  { label: 'BigQuery', value: 'bigquery' },
];

/** MySQL 线协议兼容引擎（StarRocks / Doris / TiDB）共用 MySQL 连接表单与默认端口。 */
const MYSQL_COMPATIBLE_TYPES = new Set<string>(['mysql', 'starrocks', 'doris', 'tidb']);
/** 单文件数据库只需服务器本地文件路径。 */
const FILE_DATABASE_TYPES = new Set<string>(['duckdb', 'sqlite']);
const NORMALIZED_TYPES = new Set<string>(DATA_SOURCE_TYPE_OPTIONS.map((item) => item.value));

function normalizeType(type?: DataSourceType | string | null): NormalizedDataSourceType {
  const value = String(type || '').toLowerCase();
  if (NORMALIZED_TYPES.has(value)) return value as NormalizedDataSourceType;
  return 'mysql';
}

function defaultPort(type: NormalizedDataSourceType): number | undefined {
  if (MYSQL_COMPATIBLE_TYPES.has(type)) return 3306;
  if (type === 'postgresql') return 5432;
  return undefined;
}

function optionalNumber(value: number | string | undefined): number | undefined {
  if (value === undefined || value === '') return undefined;
  const next = Number(value);
  return Number.isFinite(next) ? next : undefined;
}

function getConfigValue(config: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = config[key];
    if (typeof value === 'string') return value;
    if (typeof value === 'number') return String(value);
  }
  return undefined;
}

function configToFormValues(record: DataSourceItem): Partial<DataConnectionFormValues> {
  const config = record.config as unknown as Record<string, unknown>;
  return {
    datasource_name: record.datasource_name,
    datasource_type: normalizeType(record.datasource_type),
    description: record.description ?? undefined,
    host: getConfigValue(config, 'host'),
    port: getConfigValue(config, 'port'),
    user: getConfigValue(config, 'user'),
    password: getConfigValue(config, 'password'),
    database: getConfigValue(config, 'database'),
    endpoint: getConfigValue(config, 'endpoint'),
    project: getConfigValue(config, 'project'),
    access_key_id: getConfigValue(config, 'access_key_id'),
    access_key_secret: getConfigValue(config, 'access_key_secret'),
    sts_token: getConfigValue(config, 'sts_token'),
    path: getConfigValue(config, 'path'),
    credentials_json: getConfigValue(config, 'credentials_json'),
    location: getConfigValue(config, 'location'),
    maximum_bytes_billed: getConfigValue(config, 'maximum_bytes_billed'),
  };
}

function valuesToPayload(values: DataConnectionFormValues): DataSourceCreateRequest {
  const normalizedType = normalizeType(values.datasource_type);
  let config: DataSourceConfig;

  if (normalizedType === 'odps') {
    config = {
      endpoint: values.endpoint?.trim() || '',
      project: values.project?.trim() || '',
      access_key_id: values.access_key_id?.trim() || '',
      access_key_secret: values.access_key_secret || '',
      sts_token: values.sts_token?.trim() || null,
    };
  } else if (normalizedType === 'duckdb') {
    // DuckDB 单文件连接只有 path（后端以只读方式打开）。
    config = {
      path: values.path?.trim() || '',
    } as DataSourceConfig;
  } else if (normalizedType === 'sqlite') {
    config = {
      path: values.path?.trim() || '',
      read_only: true,
    };
  } else if (normalizedType === 'bigquery') {
    config = {
      project: values.project?.trim() || '',
      credentials_json: values.credentials_json?.trim() || '',
      location: values.location?.trim() || null,
      maximum_bytes_billed: optionalNumber(values.maximum_bytes_billed) ?? null,
    };
  } else if (normalizedType === 'postgresql') {
    config = {
      host: values.host?.trim() || '',
      port: optionalNumber(values.port) ?? 5432,
      database: values.database?.trim() || '',
      user: values.user?.trim() || '',
      password: values.password || '',
    };
  } else {
    config = {
      host: values.host?.trim() || '',
      port: optionalNumber(values.port) ?? 3306,
      database: values.database?.trim() || '',
      user: values.user?.trim() || '',
      password: values.password || '',
    };
  }

  return {
    datasource_name: values.datasource_name.trim(),
    datasource_type: normalizedType,
    description: values.description?.trim() || null,
    config,
  };
}

/** Data Connection list page backed by the current semantic-config data source API. */
export default function DataConnectionPage() {
  const { t } = useTranslation();
  const { message, modal } = useAppMessage();
  const canWrite = useHasCapability('datasource:write');
  const [searchParams, setSearchParams] = useSearchParams();
  const [form] = Form.useForm<DataConnectionFormValues>();
  const [connections, setConnections] = useState<DataSourceItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingRecord, setEditingRecord] = useState<DataSourceItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [testingForm, setTestingForm] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<DataSourceConnectionTestResult | null>(null);
  const selectedType = Form.useWatch('datasource_type', form);
  const selectedNormalizedType = useMemo(() => normalizeType(selectedType), [selectedType]);
  const isEdit = Boolean(editingRecord);

  const loadConnections = useCallback(async () => {
    setLoading(true);
    try {
      const res = await queryDataSourceList({ page: 1, size: 500 });
      setConnections(res.records);
    } catch (error) {
      console.error('Failed to load data connections:', error);
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
      datasource_type: 'mysql',
      port: 3306,
    });
    setTestResult(null);
    setDrawerOpen(true);
  }, [form]);

  useEffect(() => {
    if (searchParams.get('action') !== 'add') return;
    openAddDrawer();
    const next = new URLSearchParams(searchParams);
    next.delete('action');
    setSearchParams(next, { replace: true });
  }, [openAddDrawer, searchParams, setSearchParams]);

  const openEditDrawer = (record: DataSourceItem) => {
    setEditingRecord(record);
    form.resetFields();
    form.setFieldsValue(configToFormValues(record));
    setTestResult(null);
    setDrawerOpen(true);
  };

  const closeDrawer = () => {
    setDrawerOpen(false);
    setEditingRecord(null);
    setTestResult(null);
    form.resetFields();
  };

  const handleTypeChange = (type: NormalizedDataSourceType) => {
    form.setFieldsValue({
      host: undefined,
      port: defaultPort(type),
      user: undefined,
      password: undefined,
      database: undefined,
      endpoint: undefined,
      project: undefined,
      access_key_id: undefined,
      access_key_secret: undefined,
      sts_token: undefined,
      path: undefined,
      credentials_json: undefined,
      location: undefined,
      maximum_bytes_billed: undefined,
    });
  };

  const notifyTestResult = (raw: unknown) => {
    const result = unwrapConnectionTestResult(raw);
    setTestResult(result);
    if (result.success) {
      message.success(
        t('dataConnection.testSuccess', {
          message: result.message,
          latency: result.elapsed_ms,
        }),
      );
    } else {
      message.error(result.message || t('dataSourceManagement.testFailed'));
    }
    return result;
  };

  const handleTestFormConnection = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values);
      setTestingForm(true);
      const raw = await testDataSourceConnection({
        datasource_type: payload.datasource_type,
        config: payload.config,
      });
      notifyTestResult(raw);
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
      message.error(t('dataSourceManagement.testFailed'));
    } finally {
      setTestingForm(false);
    }
  };

  const handleTestSavedConnection = async (record: DataSourceItem) => {
    try {
      setTestingId(record.datasource_id);
      const raw = await testSavedDataSourceConnection(record.datasource_id);
      notifyTestResult(raw);
    } catch {
      message.error(t('dataSourceManagement.testFailed'));
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
        message.success(t('common.editSuccess'));
      } else {
        await createDataSource(payload);
        message.success(t('common.createSuccess'));
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
    modal.confirm({
      title: t('dataConnection.confirmDeleteTitle'),
      content: t('dataConnection.confirmDelete', { name: record.datasource_name }),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await deleteDataSource(record.datasource_id);
          message.success(t('dataConnection.deleteSuccess'));
          await reloadConnections();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: TableProps<DataSourceItem>['columns'] = [
    {
      title: t('dataConnection.type'),
      dataIndex: 'datasource_type',
      key: 'datasource_type',
      width: 220,
      render: (type: DataSourceType) => {
        const normalized = normalizeType(type);
        const meta = DATA_CONNECTION_TYPE_META[normalized];
        const fallbackLabel =
          DATA_SOURCE_TYPE_OPTIONS.find((item) => item.value === normalized)?.label ?? normalized;
        return (
          <div className={styles.typeCell}>
            <span className={styles.typeBadge} style={{ backgroundColor: meta.accent }}>
              {meta.badge}
            </span>
            <span className={styles.typeLabel}>
              {t(meta.labelKey, { defaultValue: fallbackLabel })}
            </span>
          </div>
        );
      },
    },
    {
      title: t('dataConnection.name'),
      dataIndex: 'datasource_name',
      key: 'datasource_name',
      ellipsis: true,
    },
    {
      title: <span>{t('fields.dataSourceId')}</span>,
      dataIndex: 'datasource_id',
      key: 'datasource_id',
      width: 220,
      ellipsis: true,
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 220,
      render: (_, record) => (
        <Space size="small">
          {canWrite && (
            <Button type="link" style={{ padding: 0 }} onClick={() => openEditDrawer(record)}>
              {t('common.edit')}
            </Button>
          )}
          <Button
            type="link"
            style={{ padding: 0 }}
            loading={testingId === record.datasource_id}
            onClick={() => void handleTestSavedConnection(record)}
          >
            {t('dataConnection.testConnection')}
          </Button>
          {canWrite && (
            <Button type="link" danger style={{ padding: 0 }} onClick={() => handleDelete(record)}>
              {t('common.delete')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div className={styles.dataConnectionPage}>
      <div className={styles.contentPanel}>
        <div className={styles.toolbar}>
          <span className={styles.tableTotal}>
            {t('common.total', { count: connections.length })}
          </span>
          {canWrite && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openAddDrawer}>
              {t('dataConnection.add')}
            </Button>
          )}
        </div>

        <div className={styles.tablePanel}>
          <Table<DataSourceItem>
            columns={columns}
            dataSource={connections}
            loading={loading}
            rowKey="datasource_id"
            pagination={{
              pageSize: 20,
            }}
          />
        </div>
      </div>

      <Drawer
        title={isEdit ? t('dataSourceManagement.editTitle') : t('dataConnection.addModalTitle')}
        placement="right"
        open={drawerOpen}
        onClose={closeDrawer}
        destroyOnHidden
        width={720}
        footer={
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
            <Button loading={testingForm} onClick={() => void handleTestFormConnection()}>
              {t('dataConnection.testConnection')}
            </Button>
            <Space>
              <Button onClick={closeDrawer}>{t('common.cancel')}</Button>
              <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                {t('common.confirm')}
              </Button>
            </Space>
          </div>
        }
      >
        {testResult ? (
          <Alert
            type={testResult.success ? 'success' : 'error'}
            showIcon
            style={{ marginBottom: 16 }}
            message={
              testResult.success
                ? t('dataConnection.testSuccess', {
                    message: testResult.message,
                    latency: testResult.elapsed_ms,
                  })
                : testResult.message || t('dataSourceManagement.testFailed')
            }
          />
        ) : null}
        <Form form={form} layout="vertical">
          {isEdit ? (
            <Form.Item label={t('fields.dataSourceId')}>
              <Input value={editingRecord?.datasource_id || ''} disabled />
            </Form.Item>
          ) : null}

          <Form.Item
            name="datasource_name"
            label={t('dataConnection.name')}
            rules={[{ required: true, message: t('dataConnection.nameRequired') }]}
          >
            <Input placeholder={t('dataConnection.namePlaceholder')} allowClear />
          </Form.Item>

          <Form.Item name="description" label={t('fields.description')}>
            <Input.TextArea rows={3} placeholder={t('validation.inputDescription')} />
          </Form.Item>

          <Form.Item
            name="datasource_type"
            label={t('dataConnection.type')}
            rules={[{ required: true, message: t('dataConnection.typeRequired') }]}
          >
            <Select
              placeholder={t('dataConnection.typePlaceholder')}
              options={DATA_SOURCE_TYPE_OPTIONS}
              onChange={handleTypeChange}
            />
          </Form.Item>

          {selectedNormalizedType === 'odps' ? (
            <>
              <Form.Item
                name="endpoint"
                label={t('dataConnection.endpoint')}
                rules={[{ required: true, message: t('dataConnection.endpointRequired') }]}
              >
                <Input placeholder={t('dataConnection.endpointPlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="project"
                label={t('dataConnection.projectName')}
                rules={[{ required: true, message: t('dataConnection.projectNameRequired') }]}
              >
                <Input placeholder={t('dataConnection.projectNamePlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="access_key_id"
                label={t('dataConnection.accessId')}
                rules={[{ required: true, message: t('dataConnection.accessIdRequired') }]}
              >
                <Input placeholder={t('dataConnection.accessIdPlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="access_key_secret"
                label={t('dataConnection.accessKey')}
                rules={[{ required: true, message: t('dataConnection.accessKeyRequired') }]}
              >
                <Input.Password placeholder={t('dataConnection.accessKeyPlaceholder')} />
              </Form.Item>
              <Form.Item name="sts_token" label="STS Token">
                <Input placeholder="STS Token" allowClear />
              </Form.Item>
            </>
          ) : FILE_DATABASE_TYPES.has(selectedNormalizedType) ? (
            <Form.Item
              name="path"
              label={t('dataConnection.filePath')}
              rules={[{ required: true, message: t('dataConnection.filePathRequired') }]}
            >
              <Input placeholder={t('dataConnection.filePathPlaceholder')} allowClear />
            </Form.Item>
          ) : selectedNormalizedType === 'bigquery' ? (
            <>
              <Form.Item
                name="project"
                label={t('dataConnection.projectName')}
                rules={[{ required: true, message: t('dataConnection.projectNameRequired') }]}
              >
                <Input placeholder={t('dataConnection.projectNamePlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="credentials_json"
                label="Service Account JSON"
                rules={[{ required: true, message: t('validation.fieldRequired') }]}
              >
                <Input.TextArea rows={6} placeholder='{"type": "service_account", ...}' />
              </Form.Item>
              <Form.Item name="location" label="Location">
                <Input placeholder="US" allowClear />
              </Form.Item>
              <Form.Item name="maximum_bytes_billed" label="Maximum Bytes Billed">
                <Input type="number" />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item
                name="host"
                label={t('dataConnection.host')}
                rules={[{ required: true, message: t('dataConnection.hostRequired') }]}
              >
                <Input placeholder={t('dataConnection.hostPlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="port"
                label={t('dataConnection.port')}
                rules={[{ required: true, message: t('dataConnection.portRequired') }]}
              >
                <Input type="number" placeholder={t('dataConnection.portPlaceholder')} />
              </Form.Item>
              <Form.Item
                name="user"
                label={t('dataConnection.user')}
                rules={[{ required: true, message: t('dataConnection.userRequired') }]}
              >
                <Input placeholder={t('dataConnection.userPlaceholder')} allowClear />
              </Form.Item>
              <Form.Item
                name="password"
                label={t('dataConnection.password')}
                rules={[{ required: true, message: t('dataConnection.passwordRequired') }]}
              >
                <Input.Password placeholder={t('dataConnection.passwordPlaceholder')} />
              </Form.Item>
              <Form.Item
                name="database"
                label={t('dataConnection.db')}
                rules={[{ required: true, message: t('dataConnection.dbRequired') }]}
              >
                <Input placeholder={t('dataConnection.dbPlaceholder')} allowClear />
              </Form.Item>
            </>
          )}
        </Form>
      </Drawer>
    </div>
  );
}
