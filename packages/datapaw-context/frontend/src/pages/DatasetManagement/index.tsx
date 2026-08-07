import { queryDatasetMeta, deleteDatasetMeta } from '@/services/datasetManagement';
import { DatasetManagementItem, QueryDatasetMetaParams } from '@/types/datasetManagement';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType } from '@/design';
import { Button, Popconfirm, message } from '@/design';
import React, { useRef } from 'react';
import { useNavigate } from 'react-router';
import CreateOrEditDataMetaDrawer from './components/CreateOrEditDataMetaDrawer';
import { useModal } from '@/hooks/useModal';
import { useDatasourceDomainFilter } from '@/hooks/useFilterOptions';
import { useTranslation } from 'react-i18next';

const DatasetManagement: React.FC = () => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const navigate = useNavigate();
  const { modal: createOrEditDataMetaDrawer, showModal: showCreateOrEditDataMetaDrawer } = useModal(CreateOrEditDataMetaDrawer);

  const { dataSourceOptions, domainOptions, selectDatasource } = useDatasourceDomainFilter();

  const columns: ProColumns<DatasetManagementItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
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
    { title: t('fields.datasetDescription'), dataIndex: 'dataset_comment', search: false },
    { title: t('fields.datasetType'), dataIndex: 'dataset_type' },
    { title: t('fields.sqlContent'), search: false, dataIndex: 'sql_content', ellipsis: true },
    { title: t('fields.parentDataset'), dataIndex: 'parents', search: false },
    {
      title: t('common.actions'),
      search: false,
      dataIndex: 'action',
      fixed: 'right',
      width: 200,
      render: (_, record) => (
        <>
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
          <Button
            key="column"
            type="link"
            onClick={() => navigate(`/column?datasetId=${record.dataset_id}&datasetName=${record.dataset_name}&datasourceId=${record.datasource_id}`)}
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
        columns={columns}
        request={async (params) => {
          const requestData: QueryDatasetMetaParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: toOptionalString(params.datasource_id),
            domain_id: toOptionalNumber(params.domain_id),
            dataset_name: toOptionalString(params.dataset_name),
            dataset_type: toOptionalString(params.dataset_type),
          };
          const result = await queryDatasetMeta(deleteExtraDelete(requestData));
          const list = normalizeListResponse<DatasetManagementItem>(result);
          return {
            data: list,
            success: true,
            total: extractTotal(result) || list.length,
          };
        }}
        scroll={{ x: 1500 }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        search={{ labelWidth: 'auto' }}
        toolBarRender={() => [
          <Button key="add" icon={<PlusOutlined />} type="primary" onClick={handleAddDataMeta}>
            {t('dataset.add')}
          </Button>,
        ]}
      />
      {createOrEditDataMetaDrawer}
    </>
  );
};

export default DatasetManagement;
