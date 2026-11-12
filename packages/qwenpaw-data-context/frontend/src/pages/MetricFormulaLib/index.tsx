import { METRIC_ADDITIVITY_OPTIONS } from '@/constants';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { ArrowLeftOutlined, PlusOutlined, SaveOutlined } from '@/design';
import { Button, Input, Popconfirm, Select, Space, Table, Tag, message } from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns, ProFormInstance } from '@/design';
import { useModal } from '@/hooks/useModal';
import { queryDatasetMeta } from '@/services/datasetManagement';
import {
  createMetricFormulaBatch,
  deleteMetricFormula,
  queryMetricFormulaList,
} from '@/services/metricFormulaLib';
import { queryMetricList } from '@/services/metricLib';
import { useBusinessDomainOptions, useBusinessDomainOptionsStore } from '@/store';
import { useHasCapability } from '@/store/authStore';
import type { DatasetManagementItem } from '@/types/datasetManagement';
import type { MetricLibItem } from '@/types/metricLib';
import type {
  MetricFormulaCreateRequest,
  MetricFormulaLibItem,
  QueryMetricFormulaLibParams,
} from '@/types/metricFormulaLib';
import { deleteExtraDelete, formatDatasourceLabel, toOptionalNumber } from '@/utils';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ActionMetricFormulaModal from './components/ActionMetricFormulaModal';

interface PrefillItem extends MetricFormulaCreateRequest {
  domain_id: number;
  metric_name?: string;
  domain_name?: string;
  dataset_name?: string;
}

function isPrefillItem(value: unknown): value is PrefillItem {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.domain_id === 'number' &&
    typeof item.metric_id === 'number' &&
    typeof item.dataset_id === 'number' &&
    typeof item.expr === 'string'
  );
}

const MetricFormulaLib: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const isPrefillMode = searchParams.get('prefill') === 'true';
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const { modal, showModal } = useModal(ActionMetricFormulaModal);
  const domainOptions = useBusinessDomainOptions();
  const [metricOptions, setMetricOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [datasets, setDatasets] = useState<DatasetManagementItem[]>([]);
  const [prefillData, setPrefillData] = useState<PrefillItem[]>([]);
  const [batchSaving, setBatchSaving] = useState(false);
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<QueryMetricFormulaLibParams>(searchParams, {
      domain_id: { parse: parseSemanticObjectId },
      metric_id: { parse: parseSemanticObjectId },
      dataset_id: { parse: parseSemanticObjectId },
    }),
  );

  const loadMetricOptions = async (domainId?: number, targetMetricId?: number) => {
    if (!domainId) {
      setMetricOptions([]);
      return;
    }
    const result = await queryMetricList({
      domain_id: domainId,
      page: 1,
      size: 500,
    });
    const records = [...result.records];
    if (targetMetricId && !records.some((item) => item.metric_id === targetMetricId)) {
      const match = await locateSemanticRecord({
        targetId: targetMetricId,
        getRecordId: (record: MetricLibItem) => record.metric_id,
        loadPage: (page, size) =>
          queryMetricList({
            domain_id: domainId,
            page,
            size,
          }),
      });
      if (match) records.push(match.record);
    }
    setMetricOptions(
      records.map((item) => ({
        label: item.metric_name,
        value: item.metric_id,
      })),
    );
  };

  const loadDomainOptions = () => useBusinessDomainOptionsStore.getState().fetchOptions();

  useEffect(() => {
    const initialize = async () => {
      const datasetResult = await queryDatasetMeta({
        datasource_id: datasourceId,
        page: 1,
        size: 500,
      });
      setDatasets(datasetResult.records);
      await loadDomainOptions();
      if (initialSearchValues.domain_id) {
        await loadMetricOptions(initialSearchValues.domain_id, initialSearchValues.metric_id);
      }
    };
    void initialize();
  }, [datasourceId, initialSearchValues]);

  useEffect(() => {
    if (!isPrefillMode) return;
    try {
      const parsed: unknown = JSON.parse(sessionStorage.getItem('metricFormulaPrefill') ?? '[]');
      setPrefillData(Array.isArray(parsed) ? parsed.filter(isPrefillItem) : []);
    } catch {
      setPrefillData([]);
    }
  }, [isPrefillMode]);

  const reloadMetric = (metricId: number) => {
    formRef.current?.setFieldValue('metric_id', metricId);
    actionRef.current?.reload();
  };

  const updatePrefill = <K extends keyof PrefillItem>(
    index: number,
    field: K,
    value: PrefillItem[K],
  ) => {
    setPrefillData((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, [field]: value } : item)),
    );
  };

  const handleBatchSave = async () => {
    if (prefillData.length === 0 || prefillData.some((item) => !item.expr.trim())) {
      message.warning(t('metricFormula.noDataToSave'));
      return;
    }
    setBatchSaving(true);
    try {
      await createMetricFormulaBatch(
        prefillData.map((item) => ({
          metric_id: item.metric_id,
          dataset_id: item.dataset_id,
          expr: item.expr,
          time_window: item.time_window || null,
          additivity: item.additivity ?? 'ADDITIVE',
          is_default: item.is_default ?? false,
        })),
      );
      message.success(t('metricFormula.batchSaveSuccess'));
      sessionStorage.removeItem('metricFormulaPrefill');
      setSearchParams({});
      setPrefillData([]);
    } finally {
      setBatchSaving(false);
    }
  };

  if (isPrefillMode) {
    return (
      <>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/metric-lib')}>
              {t('metricFormula.backToMetricLib')}
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={batchSaving}
              onClick={() => void handleBatchSave()}
            >
              {t('metricFormula.batchSave')}
            </Button>
          </Space>
        </div>
        <Table<PrefillItem>
          rowKey={(_, index) => String(index)}
          dataSource={prefillData}
          pagination={false}
          scroll={{ x: 1300 }}
          columns={[
            { title: t('fields.metricName'), dataIndex: 'metric_name', width: 150 },
            { title: t('fields.businessDomain'), dataIndex: 'domain_name', width: 140 },
            {
              title: t('fields.datasetName'),
              dataIndex: 'dataset_id',
              width: 180,
              render: (value: number, _item, index) => (
                <Select
                  value={value}
                  options={datasets
                    .filter((dataset) => dataset.domain_id === _item.domain_id)
                    .map((dataset) => ({
                      label: dataset.dataset_name,
                      value: dataset.dataset_id,
                    }))}
                  onChange={(next) => updatePrefill(index, 'dataset_id', next)}
                  style={{ width: '100%' }}
                />
              ),
            },
            {
              title: t('fields.formula'),
              dataIndex: 'expr',
              width: 260,
              render: (value: string, _item, index) => (
                <Input.TextArea
                  value={value}
                  rows={2}
                  onChange={(event) => updatePrefill(index, 'expr', event.target.value)}
                />
              ),
            },
            {
              title: 'time_window',
              dataIndex: 'time_window',
              width: 140,
              render: (value: string, _item, index) => (
                <Input
                  value={value}
                  onChange={(event) => updatePrefill(index, 'time_window', event.target.value)}
                />
              ),
            },
            {
              title: 'additivity',
              dataIndex: 'additivity',
              width: 180,
              render: (value, _item, index) => (
                <Select
                  value={value ?? 'ADDITIVE'}
                  options={METRIC_ADDITIVITY_OPTIONS}
                  onChange={(next) => updatePrefill(index, 'additivity', next)}
                  style={{ width: '100%' }}
                />
              ),
            },
            {
              title: 'is_default',
              dataIndex: 'is_default',
              width: 120,
              render: (value, _item, index) => (
                <Select
                  value={value ?? false}
                  options={[
                    { label: t('common.yes'), value: true },
                    { label: t('common.no'), value: false },
                  ]}
                  onChange={(next) => updatePrefill(index, 'is_default', next)}
                />
              ),
            },
          ]}
        />
      </>
    );
  }

  const columns: ProColumns<MetricFormulaLibItem>[] = [
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_name',
      search: false,
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
        onChange: (value: number) => {
          formRef.current?.setFieldsValue({ metric_id: undefined, dataset_id: undefined });
          void loadMetricOptions(value);
        },
      },
      render: (_, record) => record.domain_name,
    },
    {
      title: t('fields.metricName'),
      dataIndex: 'metric_id',
      valueType: 'select',
      fieldProps: { options: metricOptions, showSearch: true, allowClear: true },
      render: (_, record) => record.metric_name,
    },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', search: false, width: 180 },
    { title: t('fields.formula'), dataIndex: 'expr', search: false, ellipsis: true, width: 260 },
    { title: 'time_window', dataIndex: 'time_window', search: false, width: 130 },
    { title: 'additivity', dataIndex: 'additivity', search: false, width: 170 },
    {
      title: 'is_default',
      dataIndex: 'is_default',
      search: false,
      width: 110,
      render: (_, record) => (
        <Tag color={record.is_default ? 'green' : 'default'}>
          {record.is_default ? t('common.yes') : t('common.no')}
        </Tag>
      ),
    },
    {
      title: t('common.actions'),
      search: false,
      fixed: 'right',
      width: 130,
      render: (_, record) => (
        <>
          {canWrite ? (
            <Button
              type="link"
              onClick={() =>
                showModal({
                  datasourceId,
                  title: t('metricFormula.editTitle'),
                  record,
                  callback: reloadMetric,
                })
              }
            >
              {t('common.edit')}
            </Button>
          ) : null}
          {canWrite ? (
            <Popconfirm
              title={t('common.confirmDeleteRecord')}
              onConfirm={async () => {
                await deleteMetricFormula(record.id);
                message.success(t('common.deleteSuccess'));
                actionRef.current?.reload();
              }}
            >
              <Button type="link" danger>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          ) : null}
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<MetricFormulaLibItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        form={{ initialValues: initialSearchValues }}
        columns={columns}
        request={async (params) => {
          const requestData: QueryMetricFormulaLibParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId,
            domain_id: toOptionalNumber(params.domain_id),
            metric_id: toOptionalNumber(params.metric_id),
            dataset_id: toOptionalNumber(params.dataset_id),
          };
          if (
            initialSearchValues.metric_id &&
            requestData.domain_id === initialSearchValues.domain_id &&
            requestData.metric_id === initialSearchValues.metric_id
          ) {
            const result = await loadSemanticDeepLinkInOrder(
              [
                () => loadDomainOptions(),
                () =>
                  loadMetricOptions(initialSearchValues.domain_id, initialSearchValues.metric_id),
              ],
              () => queryMetricFormulaList(deleteExtraDelete(requestData)),
            );
            return { data: result.records, success: true, total: result.total };
          }
          const result = await queryMetricFormulaList(deleteExtraDelete(requestData));
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1500 }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
        }}
        search={{ labelWidth: 'auto' }}
        options={{ density: false }}
        toolBarRender={() => [
          ...(canExcel
            ? [
                <SemanticExcelActions
                  key="excel"
                  templateObject="metric_formula"
                  datasourceId={datasourceId}
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
                  onClick={() => {
                    const values = formRef.current?.getFieldsValue?.() ?? {};
                    showModal({
                      datasourceId,
                      title: t('metricFormula.addTitle'),
                      initialFilter: {
                        domain_id: values.domain_id,
                        metric_id: values.metric_id,
                      },
                      callback: reloadMetric,
                    });
                  }}
                >
                  {t('metricFormula.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {modal}
    </>
  );
};

export default MetricFormulaLib;
