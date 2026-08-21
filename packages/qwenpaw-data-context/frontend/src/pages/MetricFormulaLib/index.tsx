import { useModal } from '@/hooks/useModal';
import {
  queryMetricFormulaList,
  deleteMetricFormula,
  createMetricFormulaBatch,
} from '@/services/metricFormulaLib';
import { queryMetricList } from '@/services/metricLib';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { useDataSourceOptionsStore, useDataSourceOptions, useBusinessDomainOptionsStore, useBusinessDomainOptions } from '@/store';
import type { MetricFormulaLibItem, QueryMetricFormulaLibParams } from '@/types/metricFormulaLib';
import type { DatasetManagementItem } from '@/types/datasetManagement';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber, toOptionalString } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { PlusOutlined, ArrowLeftOutlined, SaveOutlined } from '@/design';
import { ProColumns, ProTable } from '@/design';
import type { ActionType, ProFormInstance } from '@/design';
import { Button, Popconfirm, message, Input, Select, Space, Table } from '@/design';
import React, { useRef, useState, useEffect, useMemo } from 'react';
import { useSearchParams, useNavigate } from 'react-router';
import ActionMetricFormulaModal from './components/ActionMetricFormulaModal';
import { useTranslation } from 'react-i18next';

interface PrefillItem {
  metricName: string;
  domain?: string;
  formula?: string;
  dateRange?: string;
  datasetId?: number;
  formulaEvidence?: string;
  derivedFrom?: string;
  evidenceExt?: string;
}

const MetricFormulaLib: React.FC = () => {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const prefill = searchParams.get('prefill');
  const isPrefillMode = prefill === 'true';

  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const { modal: actionModal, showModal: showActionModal } = useModal(ActionMetricFormulaModal);

  // 数据源 / 业务域 / 指标下拉选项
  const dataSourceOptions = useDataSourceOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState('');
  const searchDomainOptions = useBusinessDomainOptions(searchDatasourceId);
  const [metricOptions, setMetricOptions] = useState<{ label: string; value: number }[]>([]);

  // 数据集选项（用于批量编辑时选择数据集）
  const [datasetOptions, setDatasetOptions] = useState<DatasetManagementItem[]>([]);

  // 从地址栏读取的初始筛选条件（仅在首次进入时计算，用于回显并触发查询）
  const initialSearchValues = useMemo<{
    datasource_id?: string;
    domain_id?: number;
    metric_id?: number;
    dataset_id?: number;
  }>(() => {
    const values: {
      datasource_id?: string;
      domain_id?: number;
      metric_id?: number;
      dataset_id?: number;
    } = {};
    const ds = searchParams.get('datasource_id');
    const dm = searchParams.get('domain_id');
    const mid = searchParams.get('metric_id');
    const dsetId = searchParams.get('dataset_id');
    if (ds) values.datasource_id = ds;
    if (dm) values.domain_id = Number(dm);
    if (mid) values.metric_id = Number(mid);
    if (dsetId) values.dataset_id = Number(dsetId);
    return values;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 批量编辑数据
  const [prefillData, setPrefillData] = useState<PrefillItem[]>([]);
  const [batchSaving, setBatchSaving] = useState(false);

  // 获取数据源下拉选项
  useEffect(() => {
    // 强制刷新：每次页面初始化都重新请求数据源数据
    useDataSourceOptionsStore.getState().fetchOptions(true);
  }, []);

  // 加载数据集列表（用于批量编辑选择数据集）
  useEffect(() => {
    const fetchDatasets = async () => {
      try {
        const res = await queryDatasetMeta({ page: 1, size: 500 });
        const list = normalizeListResponse<DatasetManagementItem>(res);
        setDatasetOptions(list);
      } catch (error) {
        console.error('加载数据集列表失败:', error);
        setDatasetOptions([]);
      }
    };
    fetchDatasets();
  }, []);

  const loadSearchDomainOptions = async (dsId: string) => {
    setSearchDatasourceId(dsId);
    setMetricOptions([]);
    if (!dsId) return;
    await useBusinessDomainOptionsStore.getState().fetchOptions(dsId);
  };

  const loadMetricOptions = async (domainId: number) => {
    setMetricOptions([]);
    if (!domainId) return;
    try {
      const res = await queryMetricList({ domain_id: domainId, page: 1, size: 200 });
      const list = normalizeListResponse<MetricFormulaLibItem>(res);
      setMetricOptions(
        list
          .filter((item) => item.id != null)
          .map((item) => ({ label: item.metric_name ?? '', value: item.id })),
      );
    } catch (error) {
      console.error('加载指标列表失败:', error);
      setMetricOptions([]);
    }
  };

  // 根据地址栏携带的参数预加载级联下拉选项（业务域依赖数据源，指标依赖业务域）
  useEffect(() => {
    const ds = initialSearchValues.datasource_id;
    const dm = initialSearchValues.domain_id;
    if (!ds) return;
    (async () => {
      await loadSearchDomainOptions(ds);
      if (dm) await loadMetricOptions(dm);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 从 sessionStorage 读取预填数据
  useEffect(() => {
    if (isPrefillMode) {
      try {
        const stored = sessionStorage.getItem('metricFormulaPrefill');
        if (stored) {
          const data = JSON.parse(stored);
          setPrefillData(Array.isArray(data) ? data : []);
        }
      } catch {
        setPrefillData([]);
      }
    }
  }, [isPrefillMode]);

  // 删除单条记录
  const handleDelete = async (record: MetricFormulaLibItem) => {
    try {
      await deleteMetricFormula(record.id);
      message.success(t('common.deleteSuccess'));
      actionRef.current?.reload();
    } catch {
      // API 错误由响应拦截器统一提示
    }
  };

  // 新增口径（带入搜索栏的数据源/业务域/指标名称）
  const handleAdd = () => {
    const searchValues = formRef.current?.getFieldsValue?.() ?? {};
    showActionModal({
      title: t('metricFormula.addTitle'),
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

  // 编辑口径
  const handleEdit = (record: MetricFormulaLibItem) => {
    showActionModal({
      title: t('metricFormula.editTitle'),
      record,
      callback: () => {
        actionRef.current?.reload();
      },
    });
  };

  // 批量保存
  const handleBatchSave = async () => {
    if (prefillData.length === 0) {
      message.warning(t('metricFormula.noDataToSave'));
      return;
    }
    try {
      setBatchSaving(true);
      await createMetricFormulaBatch(prefillData);
      message.success(t('metricFormula.batchSaveSuccess'));
      // 清除 sessionStorage
      sessionStorage.removeItem('metricFormulaPrefill');
      // 切换到管理表格视图
      setSearchParams({});
      setPrefillData([]);
    } catch {
      message.error(t('metricFormula.batchSaveFailed'));
      // 保存失败时不清除 sessionStorage，避免数据丢失
    } finally {
      setBatchSaving(false);
    }
  };

  // 返回指标库
  const handleBackToMetricLib = () => {
    sessionStorage.removeItem('metricFormulaPrefill');
    navigate('/metric-lib');
  };

  // 更新批量编辑数据
  const handlePrefillChange = (index: number, field: string, value: string | number | boolean) => {
    const newData = [...prefillData];
    newData[index] = { ...newData[index], [field]: value };
    setPrefillData(newData);
  };

  // 批量编辑表格列
  const batchEditColumns = [
    {
      title: t('fields.metricName'),
      dataIndex: 'metricName',
      width: 150,
      render: (text: string) => <span>{text}</span>,
    },
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain',
      width: 100,
      render: (text: string) => <span>{text || '-'}</span>,
    },
    {
      title: t('fields.formula'),
      dataIndex: 'formula',
      width: 200,
      render: (text: string, _: PrefillItem, index: number) => (
        <Input.TextArea
          value={text}
          rows={2}
          onChange={(e) => handlePrefillChange(index, 'formula', e.target.value)}
          placeholder={t('validation.inputFormula')}
        />
      ),
    },
    {
      title: t('fields.timeRange'),
      dataIndex: 'dateRange',
      width: 120,
      render: (text: string, _: PrefillItem, index: number) => (
        <Input
          value={text}
          onChange={(e) => handlePrefillChange(index, 'dateRange', e.target.value)}
          placeholder={t('validation.inputTimeRange')}
        />
      ),
    },
    {
      title: t('fields.dataset'),
      dataIndex: 'datasetId',
      width: 180,
      render: (value: number, _: PrefillItem, index: number) => (
        <Select
          value={value}
          onChange={(val) => handlePrefillChange(index, 'datasetId', val)}
          placeholder={t('validation.selectDataset')}
          allowClear
          showSearch
          optionFilterProp="label"
          options={datasetOptions.map((item) => ({
            label: item.dataset_name,
            value: item.dataset_id,
          }))}
          style={{ width: '100%' }}
        />
      ),
    },
    {
      title: t('fields.formulaEvidence'),
      dataIndex: 'formulaEvidence',
      width: 180,
      render: (text: string, _: PrefillItem, index: number) => (
        <Input.TextArea
          value={text}
          rows={2}
          onChange={(e) => handlePrefillChange(index, 'formulaEvidence', e.target.value)}
          placeholder={t('validation.inputFormulaEvidence')}
        />
      ),
    },
    {
      title: t('fields.derivedFrom'),
      dataIndex: 'derivedFrom',
      width: 120,
      render: (text: string, _: PrefillItem, index: number) => (
        <Input
          value={text}
          onChange={(e) => handlePrefillChange(index, 'derivedFrom', e.target.value)}
          placeholder={t('validation.inputDerivedFrom')}
        />
      ),
    },
    {
      title: t('fields.evidenceExt'),
      dataIndex: 'evidenceExt',
      width: 180,
      render: (text: string, _: PrefillItem, index: number) => (
        <Input.TextArea
          value={text}
          rows={2}
          onChange={(e) => handlePrefillChange(index, 'evidenceExt', e.target.value)}
          placeholder={t('validation.inputEvidenceExt')}
        />
      ),
    },
  ];

  // 如果是预填模式，显示批量编辑区域
  if (isPrefillMode) {
    return (
      <>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={handleBackToMetricLib}>
              {t('metricFormula.backToMetricLib')}
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={handleBatchSave}
              loading={batchSaving}
              disabled={prefillData.length === 0}
            >
              {t('metricFormula.batchSave')}
            </Button>
          </Space>
        </div>
        <Table
          dataSource={prefillData}
          columns={batchEditColumns}
          rowKey={(_, index) => String(index)}
          scroll={{ x: 1400 }}
          pagination={false}
          bordered
        />
      </>
    );
  }

  // 管理表格列定义
  const columns: ProColumns<MetricFormulaLibItem>[] = [
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
        onChange: (dsId: string) => loadSearchDomainOptions(dsId),
      },
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      valueType: 'select',
      formItemProps: { rules: [{ required: true, message: t('validation.selectBusinessDomain') }] },
      fieldProps: {
        options: searchDomainOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('validation.selectBusinessDomain'),
        onChange: (domainId: number) => loadMetricOptions(domainId),
      },
      render: (_, record) => record.domain_name || '-',
    },
    {
      title: t('fields.metricName'),
      dataIndex: 'metric_id',
      valueType: 'select',
      fieldProps: {
        options: metricOptions,
        showSearch: true,
        allowClear: true,
        optionFilterProp: 'label',
        placeholder: t('metricFormula.selectMetricName'),
      },
      render: (_, record) => record.metric_name || '-',
    },
    { title: t('fields.timeRange'), dataIndex: 'date_range', search: false, width: 120 },
    {
      title: t('fields.datasetName'),
      dataIndex: 'dataset_name',
      search: false,
      width: 160,
      render: (_, record) => record.dataset_name || '-',
    },
    { title: t('fields.formula'), dataIndex: 'formula', search: false, ellipsis: true, width: 200 },
    { title: t('fields.formulaEvidence'), dataIndex: 'formula_evidence', search: false, ellipsis: true, width: 180 },
    { title: t('fields.derivedFrom'), dataIndex: 'derived_from', search: false, width: 120 },
    {
      title: t('common.actions'),
      search: false,
      dataIndex: 'action',
      fixed: 'right',
      width: 120,
      render: (_, record) => (
        <>
          <Button type="link" size="small" onClick={() => handleEdit(record)}>
            {t('common.edit')}
          </Button>
          <Popconfirm title={t('common.confirmDeleteRecord')} onConfirm={() => handleDelete(record)}>
            <Button size="small" type="link" danger>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<MetricFormulaLibItem>
        rowKey={(record: MetricFormulaLibItem) => record.id}
        actionRef={actionRef}
        formRef={formRef}
        form={{ initialValues: initialSearchValues }}
        columns={columns}
        request={async (params) => {
          const requestData: QueryMetricFormulaLibParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: toOptionalString(params.datasource_id),
            domain_id: toOptionalNumber(params.domain_id),
            metric_id: toOptionalNumber(params.metric_id),
            dataset_id: toOptionalNumber(params.dataset_id),
          };
          try {
            const result = await queryMetricFormulaList(deleteExtraDelete(requestData));
            const list = normalizeListResponse<MetricFormulaLibItem>(result);
            return {
              data: list,
              success: true,
              total: extractTotal(result) || list.length,
            };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1600 }}
        options={{ density: false }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        search={{
          labelWidth: 'auto',
        }}
        toolBarRender={() => [
          <Button key="add" icon={<PlusOutlined />} type="primary" onClick={handleAdd}>
            {t('metricFormula.add')}
          </Button>,
        ]}
      />
      {actionModal}
    </>
  );
};

export default MetricFormulaLib;
