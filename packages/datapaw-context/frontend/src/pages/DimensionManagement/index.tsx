import { useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Modal, message, Tag } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { PlusOutlined } from '@/design';
import type { DimensionItem, QueryDimensionParams } from '@/types/dimensionManagement';
import { queryDimensionList, deleteDimension } from '@/services/dimensionManagement';
import { ROUTES } from '@/router';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { useModal } from '@/hooks/useModal';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { useBusinessDomainOptions } from '@/store';
import ActionDimensionDrawer from './components/ActionDimensionDrawer';
import { useTranslation } from 'react-i18next';

const ManagementTable: React.FC = () => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const navigate = useNavigate();
  const { modal: actionDimensionDrawer, showModal: showActionDimensionDrawer } = useModal(ActionDimensionDrawer);
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains, loadDimensionNames } = useCascadeFilterOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState('');
  const searchDomainOptions = useBusinessDomainOptions(searchDatasourceId);
  const [dimensionNameOptions, setDimensionNameOptions] = useState<{ label: string; value: string }[]>([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  const loadSearchDomainOptions = async (dsId: string) => {
    setSearchDatasourceId(dsId);
    setDimensionNameOptions([]);
    formRef.current?.setFieldValue('domain_id', undefined);
    formRef.current?.setFieldValue('dimension_name', undefined);
    if (!dsId) return;
    await loadDomains(dsId);
  };

  const loadDimensionNameOptions = async (domainId: number) => {
    setDimensionNameOptions([]);
    formRef.current?.setFieldValue('dimension_name', undefined);
    if (!domainId) return;
    try {
      const options = await loadDimensionNames(domainId);
      setDimensionNameOptions(options);
    } catch (error) {
      console.error('加载维度名称选项失败:', error);
      setDimensionNameOptions([]);
    }
  };

  const handleOpenAdd = () => {
    const searchValues = formRef.current?.getFieldsValue?.() ?? {};
    showActionDimensionDrawer({
      title: t('dimension.addTitle'),
      record: undefined,
      initialFilter: {
        datasource_id: searchValues.datasource_id,
        domain_id: searchValues.domain_id,
      },
      callback: () => actionRef.current?.reload(),
    });
  };

  const handleOpenEdit = (record: DimensionItem) => {
    showActionDimensionDrawer({
      title: t('dimension.editTitle'),
      record,
      callback: () => actionRef.current?.reload(),
    });
  };

  const handleDelete = (record: DimensionItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: t('dimension.confirmDelete', { name: record.dimension_name }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteDimension(record.id);
          message.success(t('common.deleteSuccess'));
          actionRef.current?.reload();
        } catch {
          // API 错误由响应拦截器统一提示
        }
      },
    });
  };

  const handleNavigateCaliber = (record: DimensionItem) => {
    const params = new URLSearchParams();
    if (record.datasource_id) params.set('datasource_id', String(record.datasource_id));
    if (record.domain_id) params.set('domain_id', String(record.domain_id));
    if (record.dimension_name) params.set('dimension_name', record.dimension_name);
    navigate(`${ROUTES.DIMENSION_CALIBER}?${params.toString()}`);
  };

  const columns: ProColumns<DimensionItem>[] = [
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
        onChange: (domainId: number) => loadDimensionNameOptions(domainId),
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
    { title: t('fields.dataSource'), dataIndex: 'datasource_name', hideInSearch: true, width: 120, render: (_, r) => formatDatasourceLabel(r.datasource_name, r.datasource_id) },
    { title: t('fields.businessDomain'), dataIndex: 'domain_name', hideInSearch: true, width: 120, render: (_, r) => r.domain_name || '-' },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', hideInSearch: true, width: 140, render: (_, r) => r.dimension_name || '-' },
    { title: t('fields.dimensionDescription'), dataIndex: 'description', hideInSearch: true, ellipsis: true, width: 160, render: (_, r) => r.description || '-' },
    {
      title: t('fields.parentDimensionName'),
      dataIndex: 'parent_name',
      hideInSearch: true,
      width: 120,
      render: (_, r) => r.parent_name || t('dimension.rootNode'),
    },
    { title: t('fields.depth'), dataIndex: 'depth', hideInSearch: true, width: 90, render: (_, r) => r.depth ?? '-' },
    { title: t('fields.synonyms'), dataIndex: 'synonyms', hideInSearch: true, width: 160, render: (_, r) => r.synonyms || '-' },
    {
      title: t('fields.isVisible'),
      dataIndex: 'is_visible',
      hideInSearch: true,
      width: 90,
      render: (_, record) =>
        record.is_visible
          ? <Tag color="green">{t('common.yes')}</Tag>
          : <Tag color="default">{t('common.no')}</Tag>,
    },
    {
      title: t('fields.isAttribution'),
      dataIndex: 'is_attribution',
      hideInSearch: true,
      width: 90,
      render: (_, record) =>
        record.is_attribution
          ? <Tag color="green">{t('common.yes')}</Tag>
          : <Tag color="default">{t('common.no')}</Tag>,
    },
    {
      title: t('fields.enums'),
      dataIndex: 'enums',
      hideInSearch: true,
      ellipsis: true,
      width: 160,
      render: (_, r) => r.enums || '-',
    },
    {
      title: t('common.actions'),
      fixed: 'right',
      width: 180,
      hideInSearch: true,
      render: (_, record) => (
        <>
          <Button type="link" size="small" onClick={() => handleOpenEdit(record)}>
            {t('common.edit')}
          </Button>
          <Button type="link" size="small" danger onClick={() => handleDelete(record)}>
            {t('common.delete')}
          </Button>
          <Button type="link" size="small" onClick={() => handleNavigateCaliber(record)}>
            {t('routes.dimensionCaliber')}
          </Button>
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<DimensionItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        request={async (params) => {
          const { current, pageSize, datasource_id, domain_id, dimension_name } = params;
          const requestData: QueryDimensionParams = {
            page: current,
            size: pageSize,
            datasource_id: toOptionalString(datasource_id),
            domain_id: toOptionalNumber(domain_id),
            dimension_name: toOptionalString(dimension_name),
          };
          try {
            const result = await queryDimensionList(deleteExtraDelete(requestData));
            const list = normalizeListResponse<DimensionItem>(result);
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
        search={{
          labelWidth: 'auto',
          defaultCollapsed: false,
        }}
        options={{ density: false, reload: true, setting: true }}
        toolBarRender={() => [
          <Button key="add" icon={<PlusOutlined />} type="primary" onClick={handleOpenAdd}>
            {t('dimension.add')}
          </Button>,
        ]}
      />

      {actionDimensionDrawer}
    </>
  );
};

export default ManagementTable;
