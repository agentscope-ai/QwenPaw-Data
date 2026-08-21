import { useModal } from '@/hooks/useModal';
import { queryMetricList, deleteMetric } from '@/services/metricLib';
import type { MetricLibItem, QueryMetricLibParams } from '@/types/metricLib';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { ROUTES } from '@/router';
import { useNavigate } from 'react-router';
import { PlusOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { Button, Popconfirm, Tag, message } from '@/design';
import React, { useRef } from 'react';
import { useDatasourceDomainFilter } from '@/hooks/useFilterOptions';
import ActionMetricModal from './components/ActionMetricModal';
import { useTranslation } from 'react-i18next';

const MetricLib: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const { modal: actionMetricModal, showModal: showActionMetricModal } = useModal(ActionMetricModal);

  const { dataSourceOptions, domainOptions, selectDatasource } = useDatasourceDomainFilter();

  // 删除单条记录
  const handleDelete = async (record: MetricLibItem) => {
    if (!record.id) {
      message.error(t('metric.recordIdMissing'));
      return;
    }
    try {
      await deleteMetric(record.id);
      message.success(t('common.deleteSuccess'));
      actionRef.current?.reload();
    } catch {
      // API 错误由响应拦截器统一提示
    }
  };

  // 新增指标（带入搜索栏的数据源/业务域）
  const handleAdd = () => {
    const searchValues = formRef.current?.getFieldsValue?.() ?? {};
    showActionMetricModal({
      title: t('metric.addTitle'),
      record: undefined,
      initialFilter: {
        datasource_id: searchValues.datasource_id,
        domain_id: searchValues.domain_id,
      },
      callback: () => {
        actionRef.current?.reload();
      },
    });
  };

  // 编辑指标
  const handleEdit = (record: MetricLibItem) => {
    showActionMetricModal({
      title: t('metric.editTitle'),
      record,
      callback: () => {
        actionRef.current?.reload();
      },
    });
  };

  // 跳转指标口径
  const handleNavigateFormula = (record: MetricLibItem) => {
    const params = new URLSearchParams();
    if (record.datasource_id) params.set('datasource_id', String(record.datasource_id));
    if (record.domain_id) params.set('domain_id', String(record.domain_id));
    if (record.metric_name) params.set('metric_name', record.metric_name);
    navigate(`${ROUTES.METRIC_FORMULA_LIB}?${params.toString()}`);
  };

  const columns: ProColumns<MetricLibItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_id',
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectDataSource') }] },
      fieldProps: {
        options: dataSourceOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectDataSource'),
        onChange: (dsId: string) => selectDatasource(dsId),
      },
      width: 120,
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectBusinessDomain') }] },
      fieldProps: {
        options: domainOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectBusinessDomain'),
      },
      width: 120,
      render: (_, record) => record.domain_name || '-',
    },
    {
      title: t('fields.metricName'),
      dataIndex: 'metric_name',
      hideInTable: true,
      width: 150,
      fieldProps: { placeholder: t('validation.inputMetricName') },
    },
    { title: t('fields.metricName'), dataIndex: 'metric_name', search: false, width: 150 },
    { title: t('fields.description'), dataIndex: 'description', search: false, ellipsis: true, width: 160 },
    { title: t('fields.unit'), dataIndex: 'unit', search: false, width: 80 },
    {
      title: t('fields.isPolaris'),
      dataIndex: 'is_polaris',
      search: false,
      width: 100,
      render: (_, record) =>
        record.is_polaris ? <Tag color="green">{t('common.yes')}</Tag> : <Tag color="default">{t('common.no')}</Tag>,
    },
    {
      title: t('fields.showDistribution'),
      dataIndex: 'show_distribution',
      search: false,
      width: 110,
      render: (_, record) =>
        record.show_distribution ? <Tag color="green">{t('common.yes')}</Tag> : <Tag color="default">{t('common.no')}</Tag>,
    },
    {
      title: t('fields.isVisible'),
      dataIndex: 'is_visible',
      search: false,
      width: 90,
      render: (_, record) =>
        record.is_visible ? <Tag color="green">{t('common.yes')}</Tag> : <Tag color="default">{t('common.no')}</Tag>,
    },
    { title: t('fields.synonyms'), dataIndex: 'synonyms', search: false, ellipsis: true, width: 140 },
    { title: t('fields.tags'), dataIndex: 'tags', search: false, ellipsis: true, width: 120 },
    {
      title: t('common.actions'),
      search: false,
      dataIndex: 'action',
      fixed: 'right',
      width: 200,
      render: (_, record) => (
        <>
          <Button type="link" size="small" onClick={() => handleEdit(record)}>{t('common.edit')}</Button>
          <Popconfirm
            title={t('common.confirmDeleteRecord')}
            onConfirm={() => handleDelete(record)}
          >
            <Button size="small" type="link" danger>{t('common.delete')}</Button>
          </Popconfirm>
          <Button type="link" size="small" onClick={() => handleNavigateFormula(record)}>{t('routes.metricFormula')}</Button>
        </>
      ),
    },
  ];

  return (
    <>
        <ProTable<MetricLibItem>
          rowKey={(record: MetricLibItem) => record.id ?? ''}
          actionRef={actionRef}
          formRef={formRef}
          columns={columns}
          request={async (params) => {
            const { current, pageSize, datasource_id, domain_id, metric_name } = params;
            const requestData: QueryMetricLibParams = {
              page: current,
              size: pageSize,
              datasource_id: toOptionalString(datasource_id),
              domain_id: toOptionalNumber(domain_id),
              metric_name: toOptionalString(metric_name),
            };
            try {
              const result = await queryMetricList(deleteExtraDelete(requestData));
              const list = normalizeListResponse<MetricLibItem>(result);
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
          options={{ density: false }}
          pagination={{
            defaultPageSize: 10,
            showSizeChanger: true,
            showTotal: (total) => t('common.total', { count: total }),
            pageSizeOptions: ['10', '20', '50', '100'],
          }}
          search={{
            labelWidth: 'auto',
            defaultCollapsed: false,
          }}
          toolBarRender={() => [
            <Button
              key="add"
              icon={<PlusOutlined />}
              type="primary"
              onClick={handleAdd}
            >
              {t('metric.add')}
            </Button>,
          ]}
        />
        {actionMetricModal}
    </>
  );
};

export default MetricLib;
