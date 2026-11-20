import { PlusOutlined } from '@/design';
import { Button, Drawer, Form, Input, Select, Space } from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns } from '@/design';
import { useAppMessage } from '@/hooks/useAppMessage';
import {
  createDataSource,
  deleteDataSource,
  getDataSource,
  queryDataSourceList,
  queryDataSourceTypeCatalog,
  testDataSourceConnection,
  testSavedDataSourceConnection,
  updateDataSource,
} from '@/services/dataSource';
import { useDataSourceOptionsStore } from '@/store';
import { useHasCapability } from '@/store/authStore';
import type {
  DataSourceConfig,
  DataSourceConnectionTestResult,
  DataSourceCreateRequest,
  DataSourceItem,
  DataSourceQueryParams,
  DataSourceType,
  DataSourceTypeInfo,
  FormSchemaProperty,
} from '@/types/dataSource';
import { deleteExtraDelete, isFormValidationError, toOptionalString } from '@/utils';
import { unwrapConnectionTestResult } from '@/utils/connectionTest';
import { parseSemanticDatasourceId } from '@/utils/semanticDeepLink';
import { readUrlSearchValues } from '@/utils/urlSearch';
import { Alert, Typography } from 'antd';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';

const { Text } = Typography;

interface DataSourceFormValues {
  datasource_name: string;
  datasource_type: DataSourceType;
  description?: string;
  [key: string]: string | number | boolean | null | undefined;
}

const DATA_SOURCE_TYPES = [
  'bigquery',
  'doris',
  'duckdb',
  'mysql',
  'odps',
  'postgresql',
  'sqlite',
  'starrocks',
  'tidb',
] as const satisfies readonly DataSourceType[];
const DATA_SOURCE_TYPE_SET = new Set<string>(DATA_SOURCE_TYPES);

function visibleFields(info: DataSourceTypeInfo): [string, FormSchemaProperty][] {
  const schema = info.form_schema;
  if (!schema) return [];
  return Object.entries(schema.properties)
    .filter(([, property]) => property.const === undefined)
    .sort(([, left], [, right]) => (left['x-ui-order'] ?? 99) - (right['x-ui-order'] ?? 99));
}

function schemaDefaults(info: DataSourceTypeInfo | undefined): Record<string, unknown> {
  if (!info) return {};
  const defaults: Record<string, unknown> = {};
  for (const [name, property] of visibleFields(info)) {
    if (property.default !== undefined) defaults[name] = property.default;
  }
  return defaults;
}

function stringValue(values: DataSourceFormValues, key: string): string {
  const value = values[key];
  return typeof value === 'string' ? value.trim() : '';
}

function numberValue(values: DataSourceFormValues, key: string, fallback: number): number {
  const rawValue = values[key];
  if (rawValue === undefined || rawValue === null || rawValue === '') return fallback;
  const value = Number(rawValue);
  return Number.isFinite(value) ? value : fallback;
}

function nullableNumberValue(values: DataSourceFormValues, key: string): number | null {
  const rawValue = values[key];
  if (rawValue === undefined || rawValue === null || rawValue === '') return null;
  const value = Number(rawValue);
  return Number.isFinite(value) ? value : null;
}

function booleanValue(values: DataSourceFormValues, key: string, fallback: boolean): boolean {
  const value = values[key];
  if (typeof value === 'boolean') return value;
  if (value === 'true') return true;
  if (value === 'false') return false;
  return fallback;
}

function buildConfig(values: DataSourceFormValues): DataSourceConfig {
  switch (values.datasource_type) {
    case 'bigquery':
      return {
        project: stringValue(values, 'project'),
        credentials_json: stringValue(values, 'credentials_json'),
        location: stringValue(values, 'location') || null,
        maximum_bytes_billed: nullableNumberValue(values, 'maximum_bytes_billed'),
      };
    case 'odps':
      return {
        access_key_id: stringValue(values, 'access_key_id'),
        access_key_secret: stringValue(values, 'access_key_secret'),
        project: stringValue(values, 'project'),
        endpoint: stringValue(values, 'endpoint'),
        sts_token: stringValue(values, 'sts_token') || null,
      };
    case 'duckdb':
      // DuckDB 单文件连接只有 path（后端以只读方式打开）。
      return {
        path: stringValue(values, 'path'),
      } as DataSourceConfig;
    case 'postgresql':
      return {
        host: stringValue(values, 'host'),
        port: numberValue(values, 'port', 5432),
        database: stringValue(values, 'database'),
        user: stringValue(values, 'user'),
        password: stringValue(values, 'password'),
      };
    case 'sqlite':
      return {
        path: stringValue(values, 'path'),
        read_only: booleanValue(values, 'read_only', true),
      };
    // StarRocks / Doris / TiDB 走 MySQL 线协议，共用 MySQL 连接配置。
    case 'starrocks':
    case 'doris':
    case 'tidb':
    case 'mysql':
      return {
        host: stringValue(values, 'host'),
        port: numberValue(values, 'port', 3306),
        database: stringValue(values, 'database'),
        user: stringValue(values, 'user'),
        password: stringValue(values, 'password'),
      };
  }
}

function valuesToPayload(values: DataSourceFormValues): DataSourceCreateRequest {
  return {
    datasource_name: values.datasource_name.trim(),
    datasource_type: values.datasource_type,
    description: values.description?.trim() || null,
    config: buildConfig(values),
  };
}

function toDataSourceType(value: unknown): DataSourceType | undefined {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
  return DATA_SOURCE_TYPE_SET.has(normalized) ? (normalized as DataSourceType) : undefined;
}

function schemaIncludesType(property: FormSchemaProperty, type: string): boolean {
  return property.type === type || property.anyOf?.some((item) => item.type === type) === true;
}

const DataSourceManagement: React.FC = () => {
  const { t } = useTranslation();
  const { message, modal } = useAppMessage();
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<DataSourceQueryParams>(searchParams, {
      datasource_id: { parse: parseSemanticDatasourceId },
      datasource_name: {},
      datasource_type: { parse: toDataSourceType },
    }),
  );
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<DataSourceFormValues>();
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [editingRecord, setEditingRecord] = useState<DataSourceItem | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<DataSourceConnectionTestResult | null>(null);
  const [typeCatalog, setTypeCatalog] = useState<DataSourceTypeInfo[]>([]);
  const [typeCatalogLoading, setTypeCatalogLoading] = useState(true);
  const selectedType = Form.useWatch('datasource_type', form);
  const canWrite = useHasCapability('datasource:write');
  const isEdit = editingRecord != null;

  useEffect(() => {
    let active = true;
    const loadTypeCatalog = async () => {
      setTypeCatalogLoading(true);
      try {
        const catalog = await queryDataSourceTypeCatalog();
        if (active) setTypeCatalog(catalog);
      } catch {
        if (active) setTypeCatalog([]);
      } finally {
        if (active) setTypeCatalogLoading(false);
      }
    };
    void loadTypeCatalog();
    return () => {
      active = false;
    };
  }, []);

  const typeOptions = useMemo(
    () => typeCatalog.map((item) => ({ label: item.label, value: item.type })),
    [typeCatalog],
  );
  const selectedTypeInfo = useMemo(
    () => typeCatalog.find((item) => item.type === selectedType),
    [selectedType, typeCatalog],
  );
  const allConnectionFieldNames = useMemo(() => {
    const names = new Set<string>();
    for (const item of typeCatalog) {
      for (const [name] of visibleFields(item)) names.add(name);
    }
    return [...names];
  }, [typeCatalog]);

  const defaultType = typeCatalog[0]?.type;
  const typeLabel = (type: DataSourceType) =>
    typeCatalog.find((item) => item.type === toDataSourceType(type))?.label ?? type;

  const reloadTable = () => {
    useDataSourceOptionsStore.getState().reset();
    actionRef.current?.reload();
  };

  const handleOpenAddDrawer = () => {
    if (!defaultType) return;
    form.resetFields();
    setEditingRecord(null);
    const info = typeCatalog.find((item) => item.type === defaultType);
    form.setFieldsValue({
      datasource_type: defaultType,
      ...schemaDefaults(info),
    } as DataSourceFormValues);
    setTestResult(null);
    setDrawerVisible(true);
  };

  const handleOpenEditDrawer = (record: DataSourceItem) => {
    form.resetFields();
    setEditingRecord(record);
    form.setFieldsValue({
      datasource_name: record.datasource_name,
      datasource_type: record.datasource_type,
      description: record.description ?? undefined,
      ...(record.config as unknown as Record<string, unknown>),
    } as DataSourceFormValues);
    setTestResult(null);
    setDrawerVisible(true);
  };

  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    setEditingRecord(null);
    setTestResult(null);
    form.resetFields();
  };

  const handleTypeChange = (type: DataSourceType) => {
    const cleared: Record<string, unknown> = {};
    for (const name of allConnectionFieldNames) cleared[name] = undefined;
    const info = typeCatalog.find((item) => item.type === type);
    form.setFieldsValue({ ...cleared, ...schemaDefaults(info) } as DataSourceFormValues);
  };

  const notifyTestResult = (raw: unknown) => {
    const result = unwrapConnectionTestResult(raw);
    setTestResult(result);
    if (result.success) {
      message.success(
        t('dataSourceManagement.testSuccess', {
          message: result.message,
          elapsed: result.elapsed_ms,
        }),
      );
    } else {
      message.error(result.message || t('dataSourceManagement.testFailed'));
    }
  };

  const handleTestFormConnection = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values);
      setTesting(true);
      notifyTestResult(
        await testDataSourceConnection({
          datasource_type: payload.datasource_type,
          config: payload.config,
        }),
      );
    } catch (error: unknown) {
      if (!isFormValidationError(error)) message.error(t('dataSourceManagement.testFailed'));
    } finally {
      setTesting(false);
    }
  };

  const handleTestSavedConnection = async (record: DataSourceItem) => {
    try {
      setTesting(true);
      notifyTestResult(await testSavedDataSourceConnection(record.datasource_id));
    } catch {
      message.error(t('dataSourceManagement.testFailed'));
    } finally {
      setTesting(false);
    }
  };

  const handleDrawerOk = async () => {
    try {
      const payload = valuesToPayload(await form.validateFields());
      setConfirmLoading(true);
      if (editingRecord) {
        await updateDataSource(editingRecord.datasource_id, payload);
        message.success(t('common.editSuccess'));
      } else {
        await createDataSource(payload);
        message.success(t('common.createSuccess'));
      }
      handleDrawerCancel();
      reloadTable();
    } catch (error: unknown) {
      if (!isFormValidationError(error)) return;
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleDelete = (record: DataSourceItem) => {
    modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: t('dataSourceManagement.deleteTip'),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await deleteDataSource(record.datasource_id);
          message.success(t('common.deleteSuccess'));
          reloadTable();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: ProColumns<DataSourceItem>[] = [
    {
      title: t('fields.id'),
      dataIndex: 'id',
      search: false,
      width: 120,
      render: (_, record) => {
        const value = String(record.id);
        return (
          <Text
            copyable={{
              text: value,
              tooltips: [t('common.copy'), t('common.copied')],
            }}
            ellipsis={{ tooltip: value }}
            style={{ maxWidth: '100%' }}
          >
            {value}
          </Text>
        );
      },
    },
    {
      title: t('fields.dataSourceCode'),
      dataIndex: 'datasource_id',
      width: 260,
      render: (_, record) => (
        <Text
          copyable={{
            text: record.datasource_id,
            tooltips: [t('common.copy'), t('common.copied')],
          }}
          ellipsis={{ tooltip: record.datasource_id }}
          style={{ maxWidth: '100%' }}
        >
          {record.datasource_id}
        </Text>
      ),
    },
    { title: t('fields.dataSourceName'), dataIndex: 'datasource_name', width: 220 },
    {
      title: t('fields.dataSourceType'),
      dataIndex: 'datasource_type',
      valueType: 'select',
      width: 160,
      fieldProps: { options: typeOptions },
      render: (_, record) => typeLabel(record.datasource_type),
    },
    {
      title: t('fields.description'),
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      width: 240,
    },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      search: false,
      fixed: 'right',
      width: 220,
      render: (_, record) => (
        <Space>
          {canWrite ? (
            <Button type="link" onClick={() => handleOpenEditDrawer(record)} style={{ padding: 0 }}>
              {t('common.edit')}
            </Button>
          ) : null}
          <Button
            type="link"
            onClick={() => void handleTestSavedConnection(record)}
            style={{ padding: 0 }}
          >
            {t('dataConnection.testConnection')}
          </Button>
          {canWrite ? (
            <Button type="link" danger onClick={() => handleDelete(record)} style={{ padding: 0 }}>
              {t('common.delete')}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const renderConnectionFields = () => {
    if (!selectedTypeInfo) return null;
    const required = new Set(selectedTypeInfo.form_schema?.required ?? []);
    return visibleFields(selectedTypeInfo).map(([name, property]) => {
      const label = property.title?.trim() || name;
      const description = property.description?.trim();
      return (
        <Form.Item
          key={name}
          name={name}
          label={label}
          extra={description && description !== label ? description : undefined}
          rules={
            required.has(name)
              ? [{ required: true, message: t('validation.fieldRequired') }]
              : undefined
          }
        >
          {name === 'credentials_json' ? (
            <Input.TextArea rows={6} placeholder={property['x-ui-placeholder']} />
          ) : property.format === 'password' ? (
            <Input.Password placeholder={property['x-ui-placeholder']} />
          ) : schemaIncludesType(property, 'integer') ? (
            <Input type="number" placeholder={property['x-ui-placeholder']} />
          ) : schemaIncludesType(property, 'boolean') ? (
            <Select
              options={[
                { label: t('common.yes'), value: true },
                { label: t('common.no'), value: false },
              ]}
            />
          ) : (
            <Input placeholder={property['x-ui-placeholder']} allowClear />
          )}
        </Form.Item>
      );
    });
  };

  return (
    <>
      <ProTable<DataSourceItem>
        rowKey="datasource_id"
        actionRef={actionRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          const type = toDataSourceType(params.datasource_type);
          const requestData: DataSourceQueryParams = {
            datasource_id: toOptionalString(params.datasource_id),
            datasource_name: toOptionalString(params.datasource_name),
            datasource_type: type,
            page: params.current,
            size: params.pageSize,
          };
          if (
            requestData.datasource_id &&
            requestData.datasource_id === initialSearchValues.datasource_id
          ) {
            const record = await getDataSource(requestData.datasource_id);
            return { data: [record], success: true, total: 1 };
          }
          const result = await queryDataSourceList(deleteExtraDelete(requestData));
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1560 }}
        options={{ density: false }}
        search={{ labelWidth: 'auto' }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
          pageSizeOptions: ['20', '50', '100'],
        }}
        toolBarRender={() =>
          canWrite
            ? [
                <Button
                  key="add"
                  type="primary"
                  icon={<PlusOutlined />}
                  loading={typeCatalogLoading}
                  disabled={!defaultType}
                  onClick={handleOpenAddDrawer}
                >
                  {t('dataSourceManagement.add')}
                </Button>,
              ]
            : []
        }
      />

      <Drawer
        title={isEdit ? t('dataSourceManagement.editTitle') : t('dataSourceManagement.addTitle')}
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnHidden
        width={720}
        footer={
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
            <Button loading={testing} onClick={() => void handleTestFormConnection()}>
              {t('dataConnection.testConnection')}
            </Button>
            <Space>
              <Button onClick={handleDrawerCancel}>{t('common.cancel')}</Button>
              <Button type="primary" loading={confirmLoading} onClick={() => void handleDrawerOk()}>
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
                ? t('dataSourceManagement.testSuccess', {
                    message: testResult.message,
                    elapsed: testResult.elapsed_ms,
                  })
                : testResult.message || t('dataSourceManagement.testFailed')
            }
          />
        ) : null}
        <Form form={form} layout="vertical">
          {isEdit ? (
            <Form.Item label={t('fields.dataSourceCode')}>
              <Input value={editingRecord?.datasource_id ?? ''} disabled />
            </Form.Item>
          ) : null}
          <Form.Item
            name="datasource_name"
            label={t('fields.dataSourceName')}
            rules={[{ required: true, message: t('validation.inputDataSourceName') }]}
          >
            <Input placeholder={t('validation.inputDataSourceName')} allowClear />
          </Form.Item>
          <Form.Item name="description" label={t('fields.description')}>
            <Input.TextArea rows={3} placeholder={t('validation.inputDescription')} />
          </Form.Item>
          <Form.Item
            name="datasource_type"
            label={t('fields.dataSourceType')}
            rules={[{ required: true, message: t('validation.selectDataSourceType') }]}
          >
            <Select
              placeholder={t('validation.selectDataSourceType')}
              options={typeOptions}
              loading={typeCatalogLoading}
              onChange={handleTypeChange}
            />
          </Form.Item>
          {renderConnectionFields()}
        </Form>
      </Drawer>
    </>
  );
};

export default DataSourceManagement;
