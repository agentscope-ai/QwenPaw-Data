import { queryDatasetMeta, deleteDatasetMeta } from '@/services/datasetManagement';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { DatasetManagementItem, QueryDatasetMetaParams } from '@/types/datasetManagement';
import {
  deleteExtraDelete,
  formatDatasourceLabel,
  toOptionalNumber,
  toOptionalString,
} from '@/utils';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { Button, Popconfirm, message } from '@/design';
import React, { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useHasCapability } from '@/store/authStore';
import { useDataSourceOptionsStore } from '@/store';
import CreateOrEditDataMetaDrawer from './components/CreateOrEditDataMetaDrawer';
import { useModal } from '@/hooks/useModal';
import { useDatasourceDomainFilter } from '@/hooks/useFilterOptions';
import { useTranslation } from 'react-i18next';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticDatasourceId,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';

type DatasetDeepLink = QueryDatasetMetaParams & { dataset_id?: number };

const DatasetManagement: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<DatasetDeepLink>(searchParams, {
      datasource_id: { parse: parseSemanticDatasourceId },
      domain_id: { parse: parseSemanticObjectId },
      dataset_id: { parse: parseSemanticObjectId },
      dataset_name: {},
    }),
  );
  const initialSearchValues: QueryDatasetMetaParams = {
    datasource_id: datasourceId ?? deepLinkValues.datasource_id,
    domain_id: deepLinkValues.domain_id,
    dataset_name: deepLinkValues.dataset_name,
  };
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const navigate = useNavigate();
  const { modal: createOrEditDataMetaDrawer, showModal: showCreateOrEditDataMetaDrawer } = useModal(
    CreateOrEditDataMetaDrawer,
  );

  const { dataSourceOptions, domainOptions, selectDatasource } = useDatasourceDomainFilter();

  useEffect(() => {
    if (initialSearchValues.datasource_id) {
      void selectDatasource(initialSearchValues.datasource_id);
    }
  }, [initialSearchValues.datasource_id, selectDatasource]);

  const columns: ProColumns<DatasetManagementItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
      hideInSearch: Boolean(datasourceId),
      valueType: 'select',
      fieldProps: {
        options: dataSourceOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDataSource'),
        onChange: (dsId: string) => selectDatasource(dsId),
      },
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      valueType: 'select',
      fieldProps: {
        options: domainOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectBusinessDomain'),
      },
      render: (_, record) => record.domain_name || '-',
    },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name' },
    { title: t('fields.datasetDescription'), dataIndex: 'description', search: false },
    { title: t('fields.sqlContent'), search: false, dataIndex: 'source', ellipsis: true },
    {
      title: t('common.actions'),
      search: false,
      dataIndex: 'action',
      fixed: 'right',
      width: 200,
      render: (_, record) => (
        <>
          {canWrite && (
            <Button
              key="edit"
              type="link"
              onClick={() => {
                showCreateOrEditDataMetaDrawer({
                  title: t('dataset.editTitle'),
                  record,
                  callback: () => actionRef.current?.reload(),
                });
              }}
            >
              {t('common.edit')}
            </Button>
          )}
          {canWrite && (
            <Popconfirm
              key="delete"
              title={t('common.confirmDeleteRecord')}
              onConfirm={async () => {
                await deleteDatasetMeta(record.dataset_id);
                message.success(t('common.deleteSuccess'));
                actionRef.current?.reload();
              }}
            >
              <Button type="link" danger>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          )}
          <Button
            key="column"
            type="link"
            onClick={() =>
              navigate(
                `/column?dataset_id=${record.dataset_id}&datasource_id=${record.datasource_id}&domain_id=${record.domain_id}`,
              )
            }
          >
            {t('routes.columnManagement')}
          </Button>
        </>
      ),
    },
  ];

  const handleAddDataMeta = () => {
    showCreateOrEditDataMetaDrawer({
      title: t('dataset.addTitle'),
      record: undefined,
      callback: () => actionRef.current?.reload(),
    });
  };

  return (
    <>
      <ProTable<DatasetManagementItem>
        rowKey={(record: DatasetManagementItem) => record.dataset_id}
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          const requestData: QueryDatasetMetaParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId ?? toOptionalString(params.datasource_id),
            domain_id: toOptionalNumber(params.domain_id),
            dataset_name: toOptionalString(params.dataset_name),
          };
          if (
            deepLinkValues.dataset_id &&
            requestData.datasource_id === deepLinkValues.datasource_id &&
            requestData.domain_id === deepLinkValues.domain_id
          ) {
            const match = await loadSemanticDeepLinkInOrder(
              [
                () => useDataSourceOptionsStore.getState().fetchOptions(true),
                () => selectDatasource(deepLinkValues.datasource_id!),
              ],
              () =>
                locateSemanticRecord({
                  targetId: deepLinkValues.dataset_id!,
                  getRecordId: (record: DatasetManagementItem) => record.dataset_id,
                  size: params.pageSize,
                  loadPage: (page, size) =>
                    queryDatasetMeta({
                      datasource_id: deepLinkValues.datasource_id,
                      domain_id: deepLinkValues.domain_id,
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
          const result = await queryDatasetMeta(deleteExtraDelete(requestData));
          return {
            data: result.records,
            success: true,
            total: result.total,
          };
        }}
        scroll={{ x: 1500 }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
        }}
        search={{ labelWidth: 'auto' }}
        toolBarRender={() => [
          ...(canExcel
            ? [
                <SemanticExcelActions
                  key="excel"
                  templateObject="dataset"
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
                  onClick={handleAddDataMeta}
                >
                  {t('dataset.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {createOrEditDataMetaDrawer}
    </>
  );
};

export default DatasetManagement;
