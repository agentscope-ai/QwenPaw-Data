import {
  createDataSource,
  deleteDataSource,
  fetchDataSourceTypes,
  queryDataSourceList,
  testDataSourceConnection,
  testSavedDataSourceConnection,
  updateDataSource,
} from '@/services/dataSource';
import type { DataSourceTypeInfo } from '@/services/dataSource';
import type { DataSourceConfig, DataSourceItem, DataSourceQueryParams, DataSourceType } from '@/types/dataSource';
import { deleteExtraDelete, isFormValidationError, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { PlusOutlined } from '@/design';
import { Button, Drawer, Form, Input, Modal, Select, Space, message } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType } from '@/design';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useDataSourceOptionsStore } from '@/store';

interface DataSourceFormValues {
  datasource_name: string;
  datasource_type: string;
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
  path?: string;
}

/** Offline/old-backend fallback: mirrors the backend defaults before the
 * `/datasource/types` endpoint existed. */
const FALLBACK_TYPE_INFOS: DataSourceTypeInfo[] = [
  {
    type: 'mysql',
    label: 'MySQL',
    fields: [
      { name: 'host', type: 'string', required: true },
      { name: 'port', type: 'integer', required: false, default: 3306 },
      { name: 'database', type: 'string', required: true },
      { name: 'user', type: 'string', required: true },
      { name: 'password', type: 'string', required: true, secret: true },
    ],
  },
  {
    type: 'postgresql',
    label: 'PostgreSQL',
    fields: [
      { name: 'host', type: 'string', required: true },
      { name: 'port', type: 'integer', required: false, default: 5432 },
      { name: 'dbname', type: 'string', required: true },
      { name: 'user', type: 'string', required: true },
      { name: 'password', type: 'string', required: true, secret: true },
    ],
  },
  {
    type: 'odps',
    label: 'ODPS',
    fields: [
      { name: 'access_key_id', type: 'string', required: true },
      { name: 'access_key_secret', type: 'string', required: true, secret: true },
      { name: 'project', type: 'string', required: true },
      { name: 'endpoint', type: 'string', required: true },
      { name: 'sts_token', type: 'string', required: false },
    ],
  },
];

function findTypeInfo(
  typeInfos: DataSourceTypeInfo[],
  type?: DataSourceType | string | null,
): DataSourceTypeInfo {
  const value = String(type || '').toLowerCase();
  return (
    typeInfos.find((item) => item.type === value)
    ?? typeInfos.find((item) => item.type === 'mysql')
    ?? typeInfos[0]
  );
}

/** Derive the form shape from the backend field spec. */
function formShape(info: DataSourceTypeInfo): 'file' | 'odps' | 'server' {
  const names = new Set(info.fields.map((field) => field.name));
  if (names.has('path')) return 'file';
  if (names.has('endpoint')) return 'odps';
  return 'server';
}

function fieldDefault(info: DataSourceTypeInfo, name: string): unknown {
  return info.fields.find((field) => field.name === name)?.default;
}

function dbFieldKey(info: DataSourceTypeInfo): 'dbname' | 'database' {
  return info.fields.some((field) => field.name === 'dbname') ? 'dbname' : 'database';
}

function optionalNumber(value: number | string | undefined): number | undefined {
  if (value === undefined || value === '') return undefined;
  const next = Number(value);
  return Number.isFinite(next) ? next : undefined;
}

function configToFormValues(record: DataSourceItem): Partial<DataSourceFormValues> {
  const config = (record.config || {}) as Record<string, unknown>;
  return {
    datasource_name: record.datasource_name,
    datasource_type: String(record.datasource_type || 'mysql').toLowerCase(),
    host: config.host as string | undefined,
    port: config.port as number | undefined,
    user: config.user as string | undefined,
    password: config.password as string | undefined,
    dbname: config.dbname as string | undefined,
    database: config.database as string | undefined,
    endpoint: config.endpoint as string | undefined,
    project: config.project as string | undefined,
    access_key_id: config.access_key_id as string | undefined,
    access_key_secret: config.access_key_secret as string | undefined,
    sts_token: config.sts_token as string | undefined,
    path: config.path as string | undefined,
  };
}

function valuesToPayload(
  values: DataSourceFormValues,
  typeInfos: DataSourceTypeInfo[],
): Partial<DataSourceItem> {
  const info = findTypeInfo(typeInfos, values.datasource_type);
  const raw = values as unknown as Record<string, unknown>;
  const config: Record<string, unknown> = {};

  for (const field of info.fields) {
    const value = raw[field.name];
    if (field.type === 'integer') {
      config[field.name] = optionalNumber(value as number | string | undefined)
        ?? (field.default as number | undefined);
    } else if (field.secret) {
      // Secrets are submitted verbatim (no trim) so pastes survive intact.
      config[field.name] = (value as string | undefined) || '';
    } else if (field.required) {
      config[field.name] = String(value ?? '').trim();
    } else {
      config[field.name] = String(value ?? '').trim() || null;
    }
  }

  return {
    datasource_name: values.datasource_name.trim(),
    datasource_type: info.type,
    config: config as DataSourceConfig,
  };
}

const DataSourceManagement: React.FC = () => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<DataSourceFormValues>();
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [editingRecord, setEditingRecord] = useState<DataSourceItem | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const selectedType = Form.useWatch('datasource_type', form);
  const isEdit = Boolean(editingRecord);
  const [typeInfos, setTypeInfos] = useState<DataSourceTypeInfo[]>(FALLBACK_TYPE_INFOS);

  useEffect(() => {
    let cancelled = false;
    fetchDataSourceTypes()
      .then((response) => {
        const items = (response as { items?: DataSourceTypeInfo[] })?.items;
        if (!cancelled && Array.isArray(items) && items.length > 0) {
          setTypeInfos(items);
        }
      })
      .catch(() => {
        /* old backend without the endpoint: keep the fallback list */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const typeOptions = useMemo(
    () => typeInfos.map((item) => ({ label: item.label, value: item.type })),
    [typeInfos],
  );
  const selectedInfo = useMemo(
    () => findTypeInfo(typeInfos, selectedType),
    [typeInfos, selectedType],
  );
  const selectedShape = formShape(selectedInfo);
  const typeLabel = (type?: DataSourceType | string | null) =>
    typeInfos.find((item) => item.type === String(type || '').toLowerCase())?.label
    ?? String(type || '-');

  const resetDataSourceOptionCache = () => {
    useDataSourceOptionsStore.getState().reset();
  };

  const reloadTable = () => {
    resetDataSourceOptionCache();
    actionRef.current?.reload();
  };

  const handleOpenAddDrawer = () => {
    setEditingRecord(null);
    form.resetFields();
    const initial = findTypeInfo(typeInfos, 'mysql');
    form.setFieldsValue({
      datasource_type: initial.type,
      port: fieldDefault(initial, 'port') as number | undefined,
    });
    setDrawerVisible(true);
  };

  const handleOpenEditDrawer = (record: DataSourceItem) => {
    setEditingRecord(record);
    form.resetFields();
    form.setFieldsValue(configToFormValues(record));
    setDrawerVisible(true);
  };

  const handleDrawerCancel = () => {
    setDrawerVisible(false);
    setEditingRecord(null);
    form.resetFields();
  };

  const handleTypeChange = (type: string) => {
    const info = findTypeInfo(typeInfos, type);
    form.setFieldsValue({
      host: undefined,
      port: fieldDefault(info, 'port') as number | undefined,
      user: undefined,
      password: undefined,
      dbname: undefined,
      database: undefined,
      endpoint: undefined,
      project: undefined,
      access_key_id: undefined,
      access_key_secret: undefined,
      sts_token: undefined,
      path: undefined,
    });
  };

  const handleTestFormConnection = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values, typeInfos);
      setTesting(true);
      const result = await testDataSourceConnection({
        datasource_type: payload.datasource_type,
        config: payload.config,
      });
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
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
    } finally {
      setTesting(false);
    }
  };

  const handleTestSavedConnection = async (record: DataSourceItem) => {
    try {
      setTesting(true);
      const result = await testSavedDataSourceConnection(record.datasource_id);
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
    } finally {
      setTesting(false);
    }
  };

  const handleDrawerOk = async () => {
    try {
      const values = await form.validateFields();
      const payload = valuesToPayload(values, typeInfos);
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
      if (isFormValidationError(error)) return;
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleDelete = (record: DataSourceItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: t('dataSourceManagement.deleteTip'),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        await deleteDataSource(record.datasource_id);
        message.success(t('common.deleteSuccess'));
        reloadTable();
      },
    });
  };

  const columns: ProColumns<DataSourceItem>[] = [
    { title: <span>{t('fields.dataSourceId')}</span>, dataIndex: 'datasource_id', width: 220 },
    { title: t('fields.dataSourceName'), dataIndex: 'datasource_name', width: 220 },
    {
      title: t('fields.dataSourceType'),
      dataIndex: 'datasource_type',
      valueType: 'select',
      width: 160,
      fieldProps: {
        options: typeOptions,
      },
      render: (_, record) => typeLabel(record.datasource_type),
    },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      search: false,
      fixed: 'right',
      width: 220,
      render: (_, record) => (
        <Space>
          <Button type="link" onClick={() => handleOpenEditDrawer(record)} style={{ padding: 0 }}>
            {t('common.edit')}
          </Button>
          <Button type="link" onClick={() => void handleTestSavedConnection(record)} style={{ padding: 0 }}>
            {t('dataConnection.testConnection')}
          </Button>
          <Button type="link" danger onClick={() => handleDelete(record)} style={{ padding: 0 }}>
            {t('common.delete')}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <ProTable<DataSourceItem>
        rowKey="datasource_id"
        actionRef={actionRef}
        columns={columns}
        request={async (params) => {
          const requestData: DataSourceQueryParams = {
            datasource_id: toOptionalString(params.datasource_id),
            datasource_name: toOptionalString(params.datasource_name),
            datasource_type: toOptionalString(params.datasource_type),
            page: params.current,
            size: params.pageSize,
          };
          const result = await queryDataSourceList(deleteExtraDelete(requestData));
          const list = normalizeListResponse<DataSourceItem>(result);
          return {
            data: list,
            success: true,
            total: extractTotal(result) || list.length,
          };
        }}
        scroll={{ x: 1000 }}
        options={{ density: false }}
        search={{ labelWidth: 'auto' }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (total) => t('common.total', { count: total }),
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        toolBarRender={() => [
          <Button key="add" type="primary" icon={<PlusOutlined />} onClick={handleOpenAddDrawer}>
            {t('dataSourceManagement.add')}
          </Button>,
        ]}
      />

      <Drawer
        title={isEdit ? t('dataSourceManagement.editTitle') : t('dataSourceManagement.addTitle')}
        placement="right"
        open={drawerVisible}
        onClose={handleDrawerCancel}
        destroyOnHidden
        width={720}
        footer={(
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
            <Button loading={testing} onClick={() => void handleTestFormConnection()}>
              {t('dataConnection.testConnection')}
            </Button>
            <Space>
              <Button onClick={handleDrawerCancel}>
                {t('common.cancel')}
              </Button>
              <Button type="primary" loading={confirmLoading} onClick={() => void handleDrawerOk()}>
                {t('common.confirm')}
              </Button>
            </Space>
          </div>
        )}
      >
        <Form form={form} layout="vertical">
          {isEdit ? (
            <Form.Item label={t('fields.dataSourceId')}>
              <Input value={editingRecord?.datasource_id || ''} disabled />
            </Form.Item>
          ) : null}
          <Form.Item
            name="datasource_name"
            label={t('fields.dataSourceName')}
            rules={[{ required: true, message: t('validation.inputDataSourceName') }]}
          >
            <Input placeholder={t('validation.inputDataSourceName')} allowClear />
          </Form.Item>
          <Form.Item
            name="datasource_type"
            label={t('fields.dataSourceType')}
            rules={[{ required: true, message: t('validation.selectDataSourceType') }]}
          >
            <Select
              placeholder={t('validation.selectDataSourceType')}
              options={typeOptions}
              onChange={handleTypeChange}
            />
          </Form.Item>

          {selectedShape === 'file' ? (
            <Form.Item
              name="path"
              label={t('dataConnection.filePath')}
              rules={[{ required: true, message: t('dataConnection.filePathRequired') }]}
            >
              <Input placeholder={t('dataConnection.filePathPlaceholder')} allowClear />
            </Form.Item>
          ) : selectedShape === 'odps' ? (
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
                rules={[{ required: !isEdit, message: t('dataConnection.accessKeyRequired') }]}
              >
                <Input.Password
                  placeholder={isEdit ? '留空以保留现有凭证' : t('dataConnection.accessKeyPlaceholder')}
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
                rules={[{ required: !isEdit, message: t('dataConnection.passwordRequired') }]}
              >
                <Input.Password
                  placeholder={isEdit ? '留空以保留现有密码' : t('dataConnection.passwordPlaceholder')}
                />
              </Form.Item>
              <Form.Item
                name={dbFieldKey(selectedInfo)}
                label={t('dataConnection.db')}
                rules={[{ required: true, message: t('dataConnection.dbRequired') }]}
              >
                <Input placeholder={t('dataConnection.dbPlaceholder')} allowClear />
              </Form.Item>
            </>
          )}
        </Form>
      </Drawer>
    </>
  );
};

export default DataSourceManagement;
