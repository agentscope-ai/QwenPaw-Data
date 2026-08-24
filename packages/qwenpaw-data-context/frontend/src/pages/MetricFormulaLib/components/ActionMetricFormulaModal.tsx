import React, { useCallback, useEffect, useState } from 'react';
import { Drawer, Form, Input, Select, Row, Col, Button, Space, message, Modal } from '@/design';
import { FullscreenOutlined } from '@/design';
import CodeMirror from '@uiw/react-codemirror';
import { githubLight } from '@uiw/codemirror-theme-github';
import type { MetricFormulaLibItem } from '@/types/metricFormulaLib';
import type { DatasetManagementItem } from '@/types/datasetManagement';
import { createMetricFormula, updateMetricFormula } from '@/services/metricFormulaLib';
import { queryMetricList } from '@/services/metricLib';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { useDataSourceOptionsStore, useDataSourceOptions, useBusinessDomainOptionsStore, useBusinessDomainOptions } from '@/store';
import { normalizeListResponse } from '@/utils/listResponse';
import { useTranslation } from 'react-i18next';
import { isFormValidationError, omitKeys } from '@/utils';

interface ActionMetricFormulaModalProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback: () => void;
  record?: Partial<MetricFormulaLibItem>;
  initialFilter?: {
    datasource_id?: string;
    domain_id?: number;
  };
}

const DATE_RANGE_OPTIONS = [
  { label: '日', value: '日' },
  { label: '周', value: '周' },
  { label: '月', value: '月' },
  { label: '近30天', value: '近30天' },
];

/** 公式编辑器，支持行号与放大编辑 */
const FormulaEditor: React.FC<{
  value?: string;
  onChange?: (value: string) => void;
}> = ({ value, onChange }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const editor = (height: string) => (
    <CodeMirror
      value={value || ''}
      height={height}
      theme={githubLight}
      onChange={(val) => onChange?.(val)}
      placeholder={t('metricFormula.formulaPlaceholder')}
      style={{ fontSize: '14px', border: '1px solid #d9d9d9', borderRadius: '6px' }}
      basicSetup={{ lineNumbers: true }}
    />
  );

  return (
    <>
      <div style={{ position: 'relative' }}>
        <Button
          type="text"
          size="small"
          icon={<FullscreenOutlined />}
          style={{ position: 'absolute', right: 8, top: 8, zIndex: 2 }}
          onClick={() => setExpanded(true)}
        />
        {editor('160px')}
      </div>
      <Modal
        title={t('fields.formula')}
        open={expanded}
        onCancel={() => setExpanded(false)}
        footer={
          <div style={{ textAlign: 'right' }}>
            <Button type="primary" onClick={() => setExpanded(false)}>
              {t('common.confirm')}
            </Button>
          </div>
        }
        width={800}
        destroyOnClose={false}
      >
        {editor('420px')}
      </Modal>
    </>
  );
};

const ActionMetricFormulaModal: React.FC<ActionMetricFormulaModalProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
  initialFilter,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!record?.id;
  const [loading, setLoading] = useState(false);
  const dataSourceOptions = useDataSourceOptions();
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');
  const [metricOptions, setMetricOptions] = useState<{ label: string; value: number }[]>([]);
  const [datasetOptions, setDatasetOptions] = useState<{ label: string; value: number }[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const searchTimerRef = React.useRef<NodeJS.Timeout | null>(null);
  const dateRangeOptions = DATE_RANGE_OPTIONS.map((item) => ({
    ...item,
    label: t(`metricFormula.dateRange.${item.value}`),
  }));

  // 根据数据源 + 业务域查询数据集选项
  const loadDatasetOptions = useCallback(async (datasourceId?: string, domainId?: number) => {
    setDatasetOptions([]);
    if (!datasourceId || !domainId) return;
    try {
      const res = await queryDatasetMeta({ datasource_id: datasourceId, domain_id: domainId, page: 1, size: 500 });
      const list = normalizeListResponse<DatasetManagementItem>(res);
      setDatasetOptions(list.map((item) => ({ label: item.dataset_name, value: item.dataset_id })));
    } catch (error) {
      console.error('加载数据集选项失败:', error);
      setDatasetOptions([]);
    }
  }, []);

  // 获取数据源和数据集下拉选项
  useEffect(() => {
    // 强制刷新：每次打开抽屉都重新请求数据源数据
    useDataSourceOptionsStore.getState().fetchOptions(true);
  }, []);

  // 数据源变化时加载业务域，并重置业务域 / 指标 / 数据集
  const handleDatasourceChange = useCallback(async (dsId: string) => {
    form.setFieldValue('domain_id', undefined);
    form.setFieldValue('metric_id', undefined);
    form.setFieldValue('dataset_id', undefined);
    setMetricOptions([]);
    setDatasetOptions([]);
    if (!dsId) return;
    await useBusinessDomainOptionsStore.getState().fetchOptions(dsId);
  }, [form]);

  // 业务域变化时加载指标列表与数据集列表
  const handleDomainChange = useCallback(async (domainId: number) => {
    form.setFieldValue('metric_id', undefined);
    form.setFieldValue('dataset_id', undefined);
    setMetricOptions([]);
    setDatasetOptions([]);
    if (!domainId) return;
    const datasourceId = form.getFieldValue('datasource_id');
    await Promise.all([
      (async () => {
        try {
          const res = await queryMetricList({ domain_id: domainId, page: 1, size: 200 });
          const list = normalizeListResponse<MetricFormulaLibItem>(res);
          setMetricOptions(list.map((item) => ({ label: item.metric_name ?? '', value: item.id })));
        } catch (error) {
          console.error('加载指标列表失败:', error);
          setMetricOptions([]);
        }
      })(),
      loadDatasetOptions(datasourceId, domainId),
    ]);
  }, [form, loadDatasetOptions]);

  // 搜索指标名称（带防抖）
  const handleMetricSearch = (keyword: string) => {
    if (searchTimerRef.current) {
      clearTimeout(searchTimerRef.current);
    }
    searchTimerRef.current = setTimeout(async () => {
      if (!keyword) {
        setMetricOptions([]);
        return;
      }
      setSearchLoading(true);
      try {
        const res = await queryMetricList({ metric_name: keyword, page: 1, size: 50 });
        const list = normalizeListResponse<MetricFormulaLibItem>(res);
        setMetricOptions(list.map((item) => ({ label: item.metric_name ?? '', value: item.id })));
      } catch {
        setMetricOptions([]);
      } finally {
        setSearchLoading(false);
      }
    }, 300);
  };

  useEffect(() => {
    if (visible) {
      if (isEdit) {
        form.setFieldsValue(record);
        if (record?.datasource_id) {
          handleDatasourceChange(record.datasource_id).then(() => {
            if (record?.domain_id) {
              form.setFieldValue('domain_id', record.domain_id);
              handleDomainChange(record.domain_id).then(() => {
                if (record?.metric_id) {
                  form.setFieldValue('metric_id', record.metric_id);
                }
                if (record?.dataset_id) {
                  form.setFieldValue('dataset_id', record.dataset_id);
                }
              });
            }
          });
        }
      } else {
        form.resetFields();
        // 新增模式：domainOptions 会自动清空（因为 datasource_id 变为 undefined）
        setMetricOptions([]);
        // 新增模式：带入搜索栏的数据源/业务域
        if (initialFilter?.datasource_id) {
          form.setFieldValue('datasource_id', initialFilter.datasource_id);
          handleDatasourceChange(initialFilter.datasource_id).then(() => {
            if (initialFilter.domain_id) {
              form.setFieldValue('domain_id', initialFilter.domain_id);
              handleDomainChange(initialFilter.domain_id);
            }
          });
        }
      }
    }
  }, [
    visible,
    record,
    isEdit,
    initialFilter,
    form,
    handleDatasourceChange,
    handleDomainChange,
  ]);

  useEffect(() => {
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, []);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        // 编辑模式：不传关联字段
        const rest = omitKeys(values, ['datasource_id', 'domain_id', 'metric_id', 'dataset_id']);
        await updateMetricFormula(record!.id!, rest);
      } else {
        await createMetricFormula(values);
      }
      message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      onCancel();
      callback();
    } catch (err: unknown) {
      if (isFormValidationError(err)) return;
      // API 错误由响应拦截器统一提示
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    form.resetFields();
    onCancel();
  };

  return (
    <Drawer
      title={title}
      placement="right"
      open={visible}
      onClose={handleClose}
      width={720}
      destroyOnClose
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Space>
            <Button onClick={handleClose}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={handleSubmit}>{t('common.confirm')}</Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="datasource_id" label={t('fields.dataSource')} rules={[{ required: true, message: t('validation.selectDataSource') }]}>
              <Select
                placeholder={t('validation.selectDataSource')}
                options={dataSourceOptions}
                allowClear
                showSearch
                optionFilterProp="label"
                onChange={handleDatasourceChange}
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="domain_id" label={t('fields.businessDomain')} rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}>
              <Select
                placeholder={t('validation.selectBusinessDomain')}
                options={domainOptions}
                allowClear
                showSearch
                optionFilterProp="label"
                onChange={handleDomainChange}
              />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item label={t('fields.metricName')} name="metric_id" rules={[{ required: true, message: t('metricFormula.selectMetric') }]}>
              <Select
                showSearch
                filterOption={false}
                onSearch={handleMetricSearch}
                loading={searchLoading}
                options={metricOptions}
                placeholder={t('metricFormula.searchMetricName')}
                allowClear
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label={t('fields.timeRange')} name="date_range" rules={[{ required: true, message: t('metricFormula.selectTimeRange') }]}>
              <Select placeholder={t('metricFormula.selectTimeRange')} options={dateRangeOptions} allowClear />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item label={t('fields.dataset')} name="dataset_id" rules={[{ required: true, message: t('validation.selectDataset') }]}>
          <Select
            placeholder={t('validation.selectDataset')}
            options={datasetOptions}
            allowClear
            showSearch
            optionFilterProp="label"
          />
        </Form.Item>

        <Form.Item label={t('fields.formula')} name="formula">
          <FormulaEditor />
        </Form.Item>

        <Form.Item label={t('fields.formulaEvidence')} name="formula_evidence">
          <Input.TextArea rows={3} placeholder={t('metricFormula.formulaEvidencePlaceholder')} />
        </Form.Item>

        <Form.Item label={t('fields.derivedFrom')} name="derived_from">
          <Input placeholder={t('metricFormula.derivedFromPlaceholder')} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionMetricFormulaModal;
