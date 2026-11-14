import { PlusOutlined } from '@/design';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { Button, Popconfirm, Space, Tag, message } from '@/design';
import { Typography } from 'antd';
import { ProTable } from '@/design';
import type { ActionType, ProColumns, ProFormInstance } from '@/design';
import { useCascadeFilterOptions, useDataSourceFilterOptions } from '@/hooks/useFilterOptions';
import { useModal } from '@/hooks/useModal';
import { queryColumnList, deleteColumn } from '@/services/column';
import { useBusinessDomainOptions } from '@/store';
import { useHasCapability } from '@/store/authStore';
import type { ColumnItem, QueryColumnParams } from '@/types/column';
import { formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticDatasourceId,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import ActionColumnModal from './components/ActionColumnModal';
import ColumnProcessFlow from './components/ColumnProcessFlow';

type ColumnDeepLink = QueryColumnParams & { column_id?: number };

const ColumnManagement: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<ColumnDeepLink>(searchParams, {
      datasource_id: { parse: parseSemanticDatasourceId },
      domain_id: { parse: parseSemanticObjectId },
      dataset_id: { parse: parseSemanticObjectId },
      column_id: { parse: parseSemanticObjectId },
    }),
  );
  const initialSearchValues: QueryColumnParams = {
    datasource_id: datasourceId ?? deepLinkValues.datasource_id,
    domain_id: deepLinkValues.domain_id,
    dataset_id: deepLinkValues.dataset_id,
  };
  const datasetId = deepLinkValues.dataset_id;
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const { modal, showModal } = useModal(ActionColumnModal);
  const { options: dataSourceOptions, refresh: refreshDataSources } = useDataSourceFilterOptions();
  const { loadDomains, loadDatasets } = useCascadeFilterOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState(
    datasourceId ?? deepLinkValues.datasource_id ?? '',
  );
  const domainOptions = useBusinessDomainOptions(searchDatasourceId);
  const [datasetOptions, setDatasetOptions] = useState<Array<{ label: string; value: number }>>([]);

  useEffect(() => {
    const loadOptions = async () => {
      const scopeDatasourceId = datasourceId ?? deepLinkValues.datasource_id;
      if (scopeDatasourceId) {
        await loadDomains(scopeDatasourceId);
      }
      if (deepLinkValues.domain_id) {
        setDatasetOptions(await loadDatasets(deepLinkValues.domain_id, scopeDatasourceId));
      }
    };
    void loadOptions();
  }, [
    datasourceId,
    deepLinkValues.datasource_id,
    deepLinkValues.domain_id,
    loadDatasets,
    loadDomains,
  ]);

  const handleDatasourceChange = async (datasourceId?: string) => {
    setSearchDatasourceId(datasourceId ?? '');
    setDatasetOptions([]);
    formRef.current?.setFieldValue('dataset_id', undefined);
    const domainId = toOptionalNumber(formRef.current?.getFieldValue('domain_id'));
    setDatasetOptions(domainId ? await loadDatasets(domainId, datasourceId) : []);
  };

  const handleDomainChange = async (domainId?: number) => {
    formRef.current?.setFieldValue('dataset_id', undefined);
    setDatasetOptions(domainId ? await loadDatasets(domainId, searchDatasourceId) : []);
  };

  const handleDelete = async (record: ColumnItem) => {
    await deleteColumn(record.id);
    message.success(t('common.deleteSuccess'));
    actionRef.current?.reload();
  };

  const columns: ProColumns<ColumnItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
      hideInSearch: Boolean(datasourceId),
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: dataSourceOptions,
        showSearch: true,
        allowClear: true,
        onChange: (value: string) => void handleDatasourceChange(value),
      },
    },
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: domainOptions,
        showSearch: true,
        allowClear: true,
        onChange: (value: number) => void handleDomainChange(value),
      },
    },
    {
      title: t('fields.datasetName'),
      dataIndex: 'dataset_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: { options: datasetOptions, showSearch: true, allowClear: true },
    },
    {
      title: t('column.name'),
      dataIndex: 'field_name',
      search: false,
      width: 180,
      render: (_, record) => (
        <Typography.Text
          copyable={{
            text: record.field_name,
            tooltips: [t('common.copy'), t('common.copied')],
          }}
        >
          {record.field_name}
        </Typography.Text>
      ),
    },
    {
      title: t('fields.dataSourceName'),
      dataIndex: 'datasource_name',
      search: false,
      width: 180,
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    { title: t('fields.businessDomainName'), dataIndex: 'domain_name', search: false, width: 140 },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', search: false, width: 180 },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      search: false,
      width: 120,
      render: (_, record) => <Tag>{record.data_type}</Tag>,
    },
    {
      title: t('fields.description'),
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      width: 220,
    },
    { title: 'is_primary', dataIndex: 'is_primary', search: false, width: 110 },
    { title: 'is_nullable', dataIndex: 'is_nullable', search: false, width: 110 },
    {
      title: t('common.actions'),
      dataIndex: 'action',
      search: false,
      fixed: 'right',
      width: 160,
      render: (_, record) => (
        <Space size="small">
          {canWrite ? (
            <Button
              type="link"
              style={{ padding: 0 }}
              onClick={() =>
                showModal({
                  title: t('column.editTitle'),
                  record,
                  callback: () => actionRef.current?.reload(),
                })
              }
            >
              {t('common.edit')}
            </Button>
          ) : null}
          {canWrite ? (
            <Popconfirm
              title={t('common.confirmDeleteRecord')}
              onConfirm={() => void handleDelete(record)}
            >
              <Button type="link" danger style={{ padding: 0 }}>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  if (searchParams.get('mode') === 'generate' && datasetId) {
    return <ColumnProcessFlow datasetId={datasetId} />;
  }

  return (
    <>
      <ProTable<ColumnItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          const requestData: QueryColumnParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId ?? toOptionalString(params.datasource_id),
            domain_id: toOptionalNumber(params.domain_id),
            dataset_id: toOptionalNumber(params.dataset_id) ?? datasetId,
          };
          if (
            deepLinkValues.column_id &&
            requestData.datasource_id === deepLinkValues.datasource_id &&
            requestData.domain_id === deepLinkValues.domain_id &&
            requestData.dataset_id === deepLinkValues.dataset_id
          ) {
            const match = await loadSemanticDeepLinkInOrder(
              [
                refreshDataSources,
                () => loadDomains(deepLinkValues.datasource_id!),
                async () => {
                  setDatasetOptions(
                    await loadDatasets(deepLinkValues.domain_id!, deepLinkValues.datasource_id),
                  );
                },
              ],
              () =>
                locateSemanticRecord({
                  targetId: deepLinkValues.column_id!,
                  getRecordId: (record: ColumnItem) => record.id,
                  size: params.pageSize,
                  loadPage: (page, size) =>
                    queryColumnList({
                      datasource_id: deepLinkValues.datasource_id,
                      domain_id: deepLinkValues.domain_id,
                      dataset_id: deepLinkValues.dataset_id,
                      page,
                      size,
                    }),
                }),
            );
            return {
              data: match ? [match.record] : [],
              success: true,
              total: match ? 1 : 0,
            };
          }
          const result = await queryColumnList(requestData);
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1500 }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
        }}
        options={{ density: false }}
        toolBarRender={() => [
          ...(canExcel
            ? [
                <SemanticExcelActions
                  key="excel"
                  templateObject="dataset_column"
                  datasourceId={datasourceId}
                  getDatasourceId={() => formRef.current?.getFieldValue('datasource_id')}
                  onImportSuccess={() => actionRef.current?.reload()}
                />,
              ]
            : []),
          ...(canWrite
            ? [
                <Button
                  key="add"
                  icon={<PlusOutlined />}
                  type="primary"
                  onClick={() =>
                    showModal({
                      title: t('column.addTitle'),
                      record: datasetId ? { dataset_id: datasetId } : undefined,
                      callback: () => actionRef.current?.reload(),
                    })
                  }
                >
                  {t('column.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {modal}
    </>
  );
};

export default ColumnManagement;
