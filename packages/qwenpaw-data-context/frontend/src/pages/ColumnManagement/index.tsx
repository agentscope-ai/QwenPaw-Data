import { useModal } from '@/hooks/useModal';
import { queryColumnList, deleteColumn } from '@/services/column';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { useDataSourceOptionsStore, useDataSourceOptions, useBusinessDomainOptionsStore } from '@/store';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { DATA_TYPE_OPTIONS, DIMENSION_TYPE } from '@/constants';
import { ColumnItem } from '@/types/column';
import type { DatasetManagementItem } from '@/types/datasetManagement';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType } from '@/design';
import { Button, Popconfirm, message, Tag, Space, Typography, Tooltip } from '@/design';
import React, { useRef, useState, useEffect, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import ActionColumnModal from './components/ActionColumnModal';
import ColumnProcessFlow from './components/ColumnProcessFlow';
import { useTranslation } from 'react-i18next';
import { translateOptions, translateOptionValue } from '@/i18n/options';

const { Text } = Typography;

const ColumnManagement: React.FC = () => {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const mode = searchParams.get('mode');
  const datasetIdParam = searchParams.get('datasetId');
  const datasetId = datasetIdParam ? Number(datasetIdParam) : undefined;
  const datasetName = searchParams.get('datasetName') || undefined;
  const datasourceIdParam = searchParams.get('datasourceId');
  const datasourceId = datasourceIdParam || undefined;

  const actionRef = useRef<ActionType | undefined>(undefined);
  const { modal: actionColumnModal, showModal: showActionColumnModal } = useModal(ActionColumnModal);

  // 数据源 / 业务域 / 数据集 下拉选项
  const dataSourceOptions = useDataSourceOptions();
  const [domainOptions, setDomainOptions] = useState<{ label: string; value: number }[]>([]);
  const [datasetOptions, setDatasetOptions] = useState<{ label: string; value: number }[]>([]);
  const dimensionTypeOptions = useMemo(
    () => translateOptions(t, DIMENSION_TYPE, 'dimension.typeOptions'),
    [t],
  );

  // 加载数据源列表，并在数据源就绪后全量加载业务域
  useEffect(() => {
    const init = async () => {
      await useDataSourceOptionsStore.getState().fetchOptions(true);
      const dsOptions = useDataSourceOptionsStore.getState().options;
      const allDomainOptions: { label: string; value: number }[] = [];
      for (const ds of dsOptions) {
        const domainOpts = await useBusinessDomainOptionsStore.getState().fetchOptions(ds.value);
        allDomainOptions.push(...domainOpts);
      }
      // 去重
      const uniqueMap = new Map<number, string>();
      for (const opt of allDomainOptions) {
        if (!uniqueMap.has(opt.value)) uniqueMap.set(opt.value, opt.label);
      }
      setDomainOptions([...uniqueMap.entries()].map(([value, label]) => ({ label, value })));
    };
    init();
  }, []);

  // 加载数据集列表
  useEffect(() => {
    const fetchDatasets = async () => {
      try {
        const res = await queryDatasetMeta({ page: 1, size: 500 });
        const list = normalizeListResponse<DatasetManagementItem>(res);
        setDatasetOptions(list.map((item) => ({ label: item.dataset_name, value: item.dataset_id })));
      } catch (error) {
        console.error('加载数据集列表失败:', error);
        setDatasetOptions([]);
      }
    };
    fetchDatasets();
  }, []);

  // 删除单条记录
  const handleDelete = async (record: ColumnItem) => {
    try {
      await deleteColumn(record.id);
      message.success(t('common.deleteSuccess'));
      actionRef.current?.reload();
    } catch {
      // API 错误由响应拦截器统一提示
    }
  };

  // 新增列
  const handleAdd = () => {
    showActionColumnModal({
      title: t('column.addTitle'),
      record: datasetId ? { dataset_id: datasetId } : undefined,
      callback: () => {
        actionRef.current?.reload();
      },
    });
  };

  // 编辑列
  const handleEdit = (record: ColumnItem) => {
    showActionColumnModal({
      title: t('column.editTitle'),
      record,
      callback: () => {
        actionRef.current?.reload();
      },
    });
  };

  // 判断是否为生成流程模式
  const isGenerateMode = mode === 'generate' && datasetId;

  // 如果是生成模式，显示流程区
  if (isGenerateMode) {
    return <ColumnProcessFlow datasetId={datasetId} datasetName={datasetName} />;
  }

  const columns: ProColumns<ColumnItem>[] = [
     {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
      hideInTable: true,
      valueType: 'select',
      initialValue: datasourceId,
      fieldProps: {
        options: dataSourceOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDataSource'),
        onChange: async (dsId: string) => {
          // 级联加载业务域（通过 store）
          if (!dsId) return;
          await useBusinessDomainOptionsStore.getState().fetchOptions(dsId);
        },
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
        optionFilterProp: 'label',
        placeholder: t('validation.selectBusinessDomain'),
      },
    },
    {
      title: t('fields.datasetName'),
      dataIndex: 'dataset_id',
      hideInTable: true,
      valueType: 'select',
      initialValue: datasetId,
      fieldProps: {
        options: datasetOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDatasetName'),
      },
    },
    {
      title: t('column.nameWithCn'),
      dataIndex: 'column_name',
      width: 180,
      hideInSearch: true,
      render: (_, record) => (
        <div>
          <Text ellipsis style={{ fontWeight: 500, display: 'block', maxWidth: '100%' }}>{record.column_name}</Text>
          <Text type="secondary" ellipsis style={{ fontSize: 12, display: 'block', maxWidth: '100%' }}>{record.column_name_cn || '-'}</Text>
        </div>
      ),
    },
    {
      title: t('fields.dataSourceName'),
      dataIndex: 'datasource_name',
      width: 120,
      ellipsis: true,
      hideInSearch: true,
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    {
      title: t('fields.datasetName'),
      dataIndex: 'dataset_name',
      width: 120,
      hideInSearch: true,
      ellipsis: true,
      render: (_, record) => record.dataset_name || '-',
    },
    {
      title: t('fields.businessDomainName'),
      dataIndex: 'domain_name',
      width: 120,
      hideInSearch: true,
      ellipsis: true,
      render: (_, record) => record.domain_name || '-',
    },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      width: 110,
      hideInSearch: true,
      valueType: 'select',
      fieldProps: { options: DATA_TYPE_OPTIONS, placeholder: t('common.selectPlaceholder'), allowClear: true },
      render: (_, record) => {
        if (!record.data_type) return '-';
        return <Tag>{record.data_type}</Tag>;
      },
    },
    {
      title: t('column.columnType'),
      dataIndex: 'column_type',
      width: 100,
      hideInSearch: true,
      ellipsis: true,
      render: (_, record) => translateOptionValue(t, record.column_type, 'column.typeOptions'),
    },
    {
      title: t('fields.enums'),
      dataIndex: 'column_enums',
      hideInSearch: true,
      width: 120,
      ellipsis: true,
      render: (_, record) => record.column_enums || '-',
    },
    {
      title: t('column.enumDescription'),
      dataIndex: 'column_enums_description',
      hideInSearch: true,
      width: 140,
      ellipsis: true,
      render: (_, record) => record.column_enums_description || '-',
    },
    {
      title: t('fields.dimensionType'),
      dataIndex: 'dimension_type',
      width: 120,
      valueType: 'select',
      hideInSearch: true,
      ellipsis: true,
      fieldProps: { options: dimensionTypeOptions, placeholder: t('common.selectPlaceholder'), allowClear: true },
      render: (_, record) => translateOptionValue(t, record.dimension_type, 'dimension.typeOptions'),
    },
    {
      title: t('column.comment'),
      dataIndex: 'column_comment',
      hideInSearch: true,
      ellipsis: true,
      width: 120,
      render: (_, record) => record.column_comment || '-',
    },
    {
      title: t('column.samples'),
      dataIndex: 'samples',
      hideInSearch: true,
      ellipsis: true,
      width: 160,
      render: (_, record) => {
        if (!record.samples) return <Text type="secondary">-</Text>;
        return (
          <Tooltip title={record.samples}>
            <Text ellipsis style={{ maxWidth: 140 }}>{record.samples}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: t('common.actions'),
      hideInSearch: true,
      dataIndex: 'action',
      width: 140,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Tooltip title={t('common.edit')}>
            <Button
              type="link"
              onClick={() => handleEdit(record)}
              style={{ padding: 0 }}
            >{t('common.edit')}</Button>
          </Tooltip>
          <Tooltip title={t('common.copy')}>
            <Button
              type="link"
              onClick={() => {
                navigator.clipboard.writeText(record.column_name || '');
                message.success(t('column.copyNameSuccess'));
              }}
              style={{ padding: 0 }}
            >{t('common.copy')}</Button>
          </Tooltip>
          <Popconfirm
            title={t('common.confirmDeleteRecord')}
            onConfirm={() => handleDelete(record)}
          >
            <Tooltip title={t('common.delete')}>
              <Button type="link" danger style={{ padding: 0 }}>{t('common.delete')}</Button>
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
   <>
      <ProTable<ColumnItem>
        rowKey={(record: ColumnItem) => record.id}
        actionRef={actionRef}
        columns={columns}
        params={{ datasetId, datasourceId }}
        request={async (params) => {
          // ProTable 传递的 params 的 key 与 columns dataIndex 一致（snake_case）
          const { current, pageSize, datasource_id, domain_id, dataset_id, data_type, dimension_type } = params;
          const requestData = {
            page: current,
            size: pageSize,
            // 带上地址栏/筛选项中的数据源与数据集参数
            datasource_id: toOptionalString(datasource_id) || datasourceId,
            domain_id: toOptionalNumber(domain_id),
            dataset_id: toOptionalNumber(dataset_id) || datasetId,
            data_type: toOptionalString(data_type),
            dimension_type: toOptionalString(dimension_type),
          };
          try {
            const result = await queryColumnList(requestData);
            const list = normalizeListResponse<ColumnItem>(result);
            // 数据源 + 数据集同时筛选时后端可能联表返回重复行，按 id 去重
            const seen = new Set<number>();
            const deduped = list.filter((item) => {
              if (item.id == null) return true;
              if (seen.has(item.id)) return false;
              seen.add(item.id);
              return true;
            });
            const total = extractTotal(result) || deduped.length;
            return {
              data: deduped,
              success: true,
              total,
            };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1550 }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        options={{
          density: false,
        }}
        toolBarRender={() => [
          <Button
            key="add"
            icon={<PlusOutlined />}
            type="primary"
            onClick={handleAdd}
          >
            {t('column.add')}
          </Button>,
        ]}
      />
      {actionColumnModal}
      </>
  );
};

export default ColumnManagement;
