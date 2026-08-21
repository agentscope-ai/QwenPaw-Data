import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Modal, message } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { PlusOutlined } from '@/design';
import type { DimensionCaliberItem, QueryDimensionCaliberParams } from '@/types/dimensionManagement';
import { queryDimensionCaliberList, deleteDimensionCaliber } from '@/services/dimensionManagement';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { useModal } from '@/hooks/useModal';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { useBusinessDomainOptions } from '@/store';
import ActionDimensionCaliberDrawer from './components/ActionDimensionCaliberDrawer';
import { useTranslation } from 'react-i18next';

const DimensionCaliberManagement: React.FC = () => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [searchParams] = useSearchParams();
  const { modal: actionDrawer, showModal: showActionDrawer } = useModal(ActionDimensionCaliberDrawer);

  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains, loadDimensionNames, loadDatasets } = useCascadeFilterOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState('');
  const searchDomainOptions = useBusinessDomainOptions(searchDatasourceId);
  const [dimensionNameOptions, setDimensionNameOptions] = useState<{ label: string; value: string }[]>([]);
  const [searchDatasetOptions, setSearchDatasetOptions] = useState<{ label: string; value: number }[]>([]);

  const loadSearchDomainOptions = useCallback(async (dsId: string, resetFields = true) => {
    setSearchDatasourceId(dsId);
    if (resetFields) {
      setDimensionNameOptions([]);
      setSearchDatasetOptions([]);
      formRef.current?.setFieldValue('domain_id', undefined);
      formRef.current?.setFieldValue('dimension_name', undefined);
      formRef.current?.setFieldValue('dataset_id', undefined);
    }
    if (!dsId) return;
    await loadDomains(dsId);
  }, [loadDomains]);

  const loadDimensionNameOptions = useCallback(async (domainId: number, resetFields = true) => {
    if (resetFields) {
      setDimensionNameOptions([]);
      formRef.current?.setFieldValue('dimension_name', undefined);
    }
    if (!domainId) return;
    try {
      const options = await loadDimensionNames(domainId);
      setDimensionNameOptions(options);
    } catch (error) {
      console.error('加载维度名称选项失败:', error);
    }
  }, [loadDimensionNames]);

  const loadSearchDatasetOptions = useCallback(async (domainId: number, resetFields = true) => {
    if (resetFields) {
      setSearchDatasetOptions([]);
      formRef.current?.setFieldValue('dataset_id', undefined);
    }
    if (!domainId) return;
    try {
      const options = await loadDatasets(domainId);
      setSearchDatasetOptions(options);
    } catch (error) {
      console.error('加载数据集选项失败:', error);
      setSearchDatasetOptions([]);
    }
  }, [loadDatasets]);

  // 从维度管理页跳转时，自动带入地址栏筛选条件并触发查询
  useEffect(() => {
    const datasourceId = searchParams.get('datasource_id');
    const domainId = searchParams.get('domain_id');
    const dimensionName = searchParams.get('dimension_name');
    const datasetId = searchParams.get('dataset_id');

    if (!datasourceId) return;

    const applyUrlParams = async () => {
      formRef.current?.setFieldValue('datasource_id', datasourceId);
      setSearchDatasourceId(datasourceId);
      await loadSearchDomainOptions(datasourceId, false);

      if (domainId) {
        formRef.current?.setFieldValue('domain_id', Number(domainId));
        await loadDimensionNameOptions(Number(domainId), false);
        await loadSearchDatasetOptions(Number(domainId), false);
      }
      if (dimensionName) {
        formRef.current?.setFieldValue('dimension_name', dimensionName);
      }
      if (datasetId) {
        formRef.current?.setFieldValue('dataset_id', Number(datasetId));
      }

      // 提交搜索表单，确保列表请求带上以上设置的参数（reload 不会提交新设置的表单值）
      formRef.current?.submit();
    };

    applyUrlParams();
  }, [
    searchParams,
    loadSearchDomainOptions,
    loadDimensionNameOptions,
    loadSearchDatasetOptions,
  ]);

  const handleOpenAdd = () => {
    const searchValues = formRef.current?.getFieldsValue?.() ?? {};
    showActionDrawer({
      title: t('dimensionCaliber.addTitle'),
      record: undefined,
      initialFilter: {
        datasource_id: searchValues.datasource_id || undefined,
        domain_id: searchValues.domain_id || undefined,
        dimension_name: searchValues.dimension_name || undefined,
        dataset_id: searchValues.dataset_id || undefined,
      },
      callback: () => actionRef.current?.reload(),
    });
  };

  const handleOpenEdit = (record: DimensionCaliberItem) => {
    showActionDrawer({
      title: t('dimensionCaliber.editTitle'),
      record,
      callback: () => actionRef.current?.reload(),
    });
  };

  const handleDelete = (record: DimensionCaliberItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: (
        <div>
          <p>
            {t('dimensionCaliber.confirmDelete', {
              dimension: record.dimension_name || '-',
              dataset: record.dataset_name || '-',
            })}
          </p>
          <p style={{ color: '#faad14', marginBottom: 0 }}>
            {t('dimensionCaliber.deleteWarning')}
          </p>
        </div>
      ),
      okText: t('common.delete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteDimensionCaliber(record.id);
          message.success(t('common.deleteSuccess'));
          actionRef.current?.reload();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const columns: ProColumns<DimensionCaliberItem>[] = [
    // ---------- 6.6.2 查询筛选区 ----------
    {
      title: t('fields.dataSourceName'),
      dataIndex: 'datasource_id',
      hideInTable: true,
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectDataSourceName') }] },
      fieldProps: {
        options: dataSourceOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDataSourceName'),
        onChange: (dsId: string) => loadSearchDomainOptions(dsId),
      },
    },
    {
      title: t('fields.businessDomainName'),
      dataIndex: 'domain_id',
      hideInTable: true,
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectBusinessDomainName') }] },
      fieldProps: {
        options: searchDomainOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectBusinessDomainName'),
        onChange: (domainId: number) => {
          loadDimensionNameOptions(domainId);
          loadSearchDatasetOptions(domainId);
        },
      },
    },
    {
      title: t('fields.dimensionName'),
      dataIndex: 'dimension_name',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: dimensionNameOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDimensionName'),
      },
    },
    {
      title: t('fields.dataset'),
      dataIndex: 'dataset_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: searchDatasetOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDataset'),
      },
    },
    // ---------- 6.6.3 维度口径列表 ----------
    { title: t('fields.dataSource'), dataIndex: 'datasource_name', hideInSearch: true, width: 120, ellipsis: true, render: (_, r) => formatDatasourceLabel(r.datasource_name, r.datasource_id) },
    { title: t('fields.businessDomain'), dataIndex: 'domain_name', hideInSearch: true, width: 120, ellipsis: true, render: (_, r) => r.domain_name || '-' },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', hideInSearch: true, width: 180, ellipsis: true, render: (_, r) => r.dataset_name || '-' },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', hideInSearch: true, width: 120, ellipsis: true, render: (_, r) => r.dimension_name || '-' },
    {
      title: t('fields.calculateExpression'),
      dataIndex: 'calculate_expr',
      hideInSearch: true,
      ellipsis: true,
      width: 240,
      render: (_, r) => r.calculate_expr || '-',
    },
    { title: t('fields.dimensionType'), dataIndex: 'dimension_type', hideInSearch: true, width: 120, render: (_, r) => r.dimension_type || '-' },
    { title: t('fields.dimensionDataType'), dataIndex: 'data_type', hideInSearch: true, width: 120, render: (_, r) => r.data_type || '-' },
    {
      title: t('common.actions'),
      valueType: 'option',
      fixed: 'right',
      width: 130,
      hideInSearch: true,
      render: (_, record) => [
        <Button key="edit" type="link" size="small" onClick={() => handleOpenEdit(record)}>
          {t('common.edit')}
        </Button>,
        <Button key="delete" type="link" size="small" danger onClick={() => handleDelete(record)}>
          {t('common.delete')}
        </Button>,
      ],
    },
  ];

  return (
    <>
      <ProTable<DimensionCaliberItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        request={async (params) => {
          const { current, pageSize, datasource_id, domain_id, dataset_id, dimension_name } = params;
          const requestData: QueryDimensionCaliberParams = {
            page: current,
            size: pageSize,
            datasource_id: toOptionalString(datasource_id),
            domain_id: toOptionalNumber(domain_id),
            dataset_id: toOptionalNumber(dataset_id),
            dimension_name: toOptionalString(dimension_name),
          };
          try {
            const result = await queryDimensionCaliberList(deleteExtraDelete(requestData));
            const list = normalizeListResponse<DimensionCaliberItem>(result);
            return {
              data: list,
              success: true,
              total: extractTotal(result) || list.length,
            };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1500 }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showQuickJumper: true,
          showTotal: (total) => t('common.total', { count: total }),
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        search={{ labelWidth: 'auto', defaultCollapsed: false }}
        options={{ density: false, reload: true, setting: true }}
        toolBarRender={() => [
          <Button key="add" type="primary" icon={<PlusOutlined />} onClick={handleOpenAdd}>
            {t('dimensionCaliber.add')}
          </Button>,
        ]}
      />

      {actionDrawer}
    </>
  );
};

export default DimensionCaliberManagement;
