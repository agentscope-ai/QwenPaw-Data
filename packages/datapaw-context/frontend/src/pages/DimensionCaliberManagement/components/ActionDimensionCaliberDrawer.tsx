import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Drawer, Form,  Button, Space, message, Select, Row, Col, Modal } from '@/design';
import { FullscreenOutlined } from '@/design';
import CodeMirror from '@uiw/react-codemirror';
import { githubLight } from '@uiw/codemirror-theme-github';
import {
  createDimensionCaliber,
  updateDimensionCaliber,
} from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import type { DimensionCaliberItem } from '@/types/dimensionManagement';
import { DIMENSION_TYPE, DATA_TYPE_OPTIONS } from '@/constants';
import { useTranslation } from 'react-i18next';
import { translateOptions } from '@/i18n/options';

interface ActionDimensionCaliberDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DimensionCaliberItem;
  initialFilter?: {
    datasource_id?: string;
    domain_id?: number;
    dimension_name?: string;
    dataset_id?: number;
  };
}

/** 计算表达式编辑器，支持行号与放大编辑 */
const CalculateExprEditor: React.FC<{
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
      placeholder={t('validation.inputCalculateExpression')}
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
        {editor('140px')}
      </div>
      <Modal
        title={t('fields.calculateExpression')}
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

const ActionDimensionCaliberDrawer: React.FC<ActionDimensionCaliberDrawerProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
  initialFilter,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const dimensionTypeOptions = useMemo(
    () => translateOptions(t, DIMENSION_TYPE, 'dimension.typeOptions'),
    [t],
  );
  const isEdit = !!record?.id;
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const [loading, setLoading] = useState(false);
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains, loadDatasets, loadDimensionIds } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');
  const [datasetOptions, setDatasetOptions] = useState<{ label: string; value: number }[]>([]);
  const [dimensionOptions, setDimensionOptions] = useState<{ label: string; value: number }[]>([]);

  const fetchDomainOptions = useCallback(async (dsId: string, resetFields = true) => {
    if (resetFields && !isEdit) {
      form.setFieldValue('domain_id', undefined);
      form.setFieldValue('dataset_id', undefined);
      form.setFieldValue('dimension_id', undefined);
    }
    if (!resetFields) {
      setDatasetOptions([]);
      setDimensionOptions([]);
    }
    if (!dsId) return;
    await loadDomains(dsId);
  }, [form, isEdit, loadDomains]);

  const fetchDatasetOptions = useCallback(async (domainId: number, resetFields = true) => {
    if (resetFields && !isEdit) {
      form.setFieldValue('dataset_id', undefined);
      form.setFieldValue('dimension_id', undefined);
    }
    setDatasetOptions([]);
    if (!resetFields) {
      setDimensionOptions([]);
    }
    if (!domainId) return;
    try {
      const options = await loadDatasets(domainId);
      setDatasetOptions(options);
    } catch (error) {
      console.error('加载数据集选项失败:', error);
    }
  }, [form, isEdit, loadDatasets]);

  const fetchDimensionOptions = useCallback(async (domainId: number, resetFields = true) => {
    if (resetFields && !isEdit) {
      form.setFieldValue('dimension_id', undefined);
    }
    setDimensionOptions([]);
    if (!domainId) return;
    try {
      const options = await loadDimensionIds(domainId);
      setDimensionOptions(options);
    } catch (error) {
      console.error('加载维度选项失败:', error);
      setDimensionOptions([]);
    }
  }, [form, isEdit, loadDimensionIds]);

  useEffect(() => {
    if (!visible) return;

    if (record) {
      form.setFieldsValue({
        datasource_id: record.datasource_id,
        domain_id: record.domain_id,
        dataset_id: record.dataset_id,
        dimension_id: record.dimension_id,
        calculate_expr: record.calculate_expr,
        dimension_type: record.dimension_type,
        data_type: record.data_type,
      });
      if (record.datasource_id) {
        fetchDomainOptions(record.datasource_id, false).then(() => {
          if (record.domain_id) {
            Promise.all([
              fetchDatasetOptions(record.domain_id, false),
              fetchDimensionOptions(record.domain_id, false),
            ]);
          }
        });
      }
    } else {
      form.resetFields();
      // domainOptions 会自动清空（因为 datasource_id 变为 undefined）
      setDatasetOptions([]);
      setDimensionOptions([]);

      const initFromFilter = async () => {
        if (!initialFilter?.datasource_id) return;
        form.setFieldValue('datasource_id', initialFilter.datasource_id);
        await fetchDomainOptions(initialFilter.datasource_id, false);
        if (initialFilter.domain_id) {
          form.setFieldValue('domain_id', initialFilter.domain_id);
          await Promise.all([
            fetchDatasetOptions(initialFilter.domain_id, false),
            fetchDimensionOptions(initialFilter.domain_id, false),
          ]);
          if (initialFilter.dataset_id) {
            form.setFieldValue('dataset_id', initialFilter.dataset_id);
          }
        }
      };
      initFromFilter();
    }
  }, [
    visible,
    record,
    initialFilter,
    form,
    fetchDomainOptions,
    fetchDatasetOptions,
    fetchDimensionOptions,
  ]);

  useEffect(() => {
    if (!visible || record || !initialFilter?.dimension_name || dimensionOptions.length === 0) return;
    const matched = dimensionOptions.find((o) => o.label === initialFilter.dimension_name);
    if (matched) {
      form.setFieldValue('dimension_id', matched.value);
    }
  }, [visible, record, initialFilter, dimensionOptions, form]);

  const handleDatasourceChange = (dsId: string) => {
    if (!isEdit) fetchDomainOptions(dsId, true);
  };

  const handleDomainChange = (domainId: number) => {
    if (!isEdit) {
      fetchDatasetOptions(domainId, true);
      fetchDimensionOptions(domainId, true);
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        const { calculate_expr, dimension_type, data_type } = values;
        await updateDimensionCaliber(record!.id, { calculate_expr, dimension_type, data_type });
        message.success(t('common.editSuccess'));
      } else {
        const payload = { ...values, dimension_type: null };
        await createDimensionCaliber(payload);
        message.success(t('common.createSuccess'));
      }
      callback?.();
      onCancel();
    } catch (err: unknown) {
      if ((err as { errorFields?: unknown })?.errorFields) return;
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
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={handleClose}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={handleSubmit}>
              {t('common.confirm')}
            </Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="datasource_id"
              label={t('fields.dataSource')}
              rules={[{ required: true, message: t('validation.selectDataSource') }]}
            >
              <Select
                placeholder={t('validation.selectDataSource')}
                options={dataSourceOptions}
                allowClear={!isEdit}
                showSearch
                optionFilterProp="label"
                disabled={isEdit}
                onChange={handleDatasourceChange}
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="domain_id"
              label={t('fields.businessDomain')}
              rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}
            >
              <Select
                placeholder={t('validation.selectBusinessDomain')}
                options={domainOptions}
                allowClear={!isEdit}
                showSearch
                optionFilterProp="label"
                disabled={isEdit}
                onChange={handleDomainChange}
              />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item name="dataset_id" label={t('fields.datasetName')}>
          <Select
            placeholder={t('validation.selectDatasetName')}
            options={datasetOptions}
            allowClear={!isEdit}
            showSearch
            optionFilterProp="label"
            disabled={isEdit}
          />
        </Form.Item>

        <Form.Item
          name="dimension_id"
          label={t('fields.dimensionName')}
          rules={[{ required: true, message: t('validation.selectDimensionName') }]}
        >
          <Select
            placeholder={t('validation.selectDimensionName')}
            options={dimensionOptions}
            allowClear={!isEdit}
            showSearch
            optionFilterProp="label"
            disabled={isEdit}
          />
        </Form.Item>

        <Form.Item name="calculate_expr" label={t('fields.calculateExpression')}>
          <CalculateExprEditor />
        </Form.Item>

        <Row gutter={16}>
          {isEdit && (
            <Col span={12}>
              <Form.Item name="dimension_type" label={t('fields.dimensionType')}>
                <Select placeholder={t('validation.selectDimensionType')} options={dimensionTypeOptions} allowClear />
              </Form.Item>
            </Col>
          )}
          <Col span={12}>
            <Form.Item name="data_type" label={t('fields.dimensionDataType')}>
              <Select placeholder={t('validation.selectDimensionDataType')} options={DATA_TYPE_OPTIONS} allowClear />
            </Form.Item>
          </Col>
        </Row>
      </Form>
    </Drawer>
  );
};

export default ActionDimensionCaliberDrawer;
