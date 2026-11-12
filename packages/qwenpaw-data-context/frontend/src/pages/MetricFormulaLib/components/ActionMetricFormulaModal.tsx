import { METRIC_ADDITIVITY_OPTIONS } from '@/constants';
import { FullscreenOutlined } from '@/design';
import { Button, Col, Drawer, Form, Input, Modal, Row, Select, Space, message } from '@/design';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { createMetricFormula, updateMetricFormula } from '@/services/metricFormulaLib';
import { queryMetricList } from '@/services/metricLib';
import { useBusinessDomainOptions, useBusinessDomainOptionsStore } from '@/store';
import type { MetricFormulaCreateRequest, MetricFormulaLibItem } from '@/types/metricFormulaLib';
import { isFormValidationError } from '@/utils';
import { githubLight } from '@uiw/codemirror-theme-github';
import CodeMirror from '@uiw/react-codemirror';
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface ActionMetricFormulaModalProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback: (metricId: number) => void;
  record?: Partial<MetricFormulaLibItem>;
  datasourceId?: string;
  initialFilter?: { domain_id?: number; metric_id?: number };
}

const FormulaEditor: React.FC<{
  value?: string;
  onChange?: (value: string) => void;
}> = ({ value, onChange }) => {
  const [expanded, setExpanded] = useState(false);
  const editor = (height: string) => (
    <CodeMirror
      value={value ?? ''}
      height={height}
      theme={githubLight}
      onChange={(next) => onChange?.(next)}
      basicSetup={{ lineNumbers: true }}
      style={{ border: '1px solid #d9d9d9', borderRadius: 6 }}
    />
  );
  return (
    <>
      <div style={{ position: 'relative' }}>
        <Button
          type="text"
          icon={<FullscreenOutlined />}
          style={{ position: 'absolute', right: 8, top: 8, zIndex: 2 }}
          onClick={() => setExpanded(true)}
        />
        {editor('160px')}
      </div>
      <Modal open={expanded} onCancel={() => setExpanded(false)} footer={null} width={800}>
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
  datasourceId,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<MetricFormulaCreateRequest & { domain_id: number }>();
  const isEdit = record?.id != null;
  const [loading, setLoading] = useState(false);
  const domainOptions = useBusinessDomainOptions();
  const [metricOptions, setMetricOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [datasetOptions, setDatasetOptions] = useState<Array<{ label: string; value: number }>>([]);

  const loadChildren = useCallback(
    async (domainId?: number) => {
      if (!domainId) {
        setMetricOptions([]);
        setDatasetOptions([]);
        return;
      }
      const [metrics, datasets] = await Promise.all([
        queryMetricList({ domain_id: domainId, page: 1, size: 500 }),
        queryDatasetMeta({
          datasource_id: datasourceId,
          domain_id: domainId,
          page: 1,
          size: 500,
        }),
      ]);
      setMetricOptions(
        metrics.records.map((item) => ({
          label: item.metric_name,
          value: item.metric_id,
        })),
      );
      setDatasetOptions(
        datasets.records.map((item) => ({ label: item.dataset_name, value: item.dataset_id })),
      );
    },
    [datasourceId],
  );

  useEffect(() => {
    if (!visible) return;
    const initialize = async () => {
      form.resetFields();
      form.setFieldsValue({
        additivity: 'ADDITIVE',
        is_default: false,
      } as MetricFormulaCreateRequest);
      const domainId = record?.domain_id ?? initialFilter?.domain_id;
      await useBusinessDomainOptionsStore.getState().fetchOptions();
      if (domainId) {
        form.setFieldValue('domain_id', domainId);
        await loadChildren(domainId);
      }
      if (record) {
        form.setFieldsValue(record as MetricFormulaCreateRequest);
      } else if (initialFilter?.metric_id) {
        form.setFieldValue('metric_id', initialFilter.metric_id);
      }
    };
    void initialize();
  }, [form, initialFilter, loadChildren, record, visible]);

  const handleDomainChange = async (domainId?: number) => {
    form.setFieldsValue({ metric_id: undefined, dataset_id: undefined });
    await loadChildren(domainId);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        await updateMetricFormula(record!.id!, {
          expr: values.expr,
          time_window: values.time_window || null,
          additivity: values.additivity ?? 'ADDITIVE',
          is_default: values.is_default ?? false,
        });
      } else {
        await createMetricFormula(values);
      }
      message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      callback(values.metric_id);
      onCancel();
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
    } finally {
      setLoading(false);
    }
  };

  return (
    <Drawer
      title={title}
      open={visible}
      onClose={onCancel}
      width={720}
      destroyOnClose
      footer={
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={onCancel}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={() => void handleSubmit()}>
              {t('common.confirm')}
            </Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        <Row gutter={16}>
          <Col span={24}>
            <Form.Item
              name="domain_id"
              label={t('fields.businessDomain')}
              rules={[{ required: true }]}
            >
              <Select
                options={domainOptions}
                showSearch
                disabled={isEdit}
                onChange={(value) => void handleDomainChange(value)}
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="metric_id" label={t('fields.metricName')} rules={[{ required: true }]}>
          <Select options={metricOptions} showSearch disabled={isEdit} />
        </Form.Item>
        <Form.Item name="dataset_id" label={t('fields.datasetName')} rules={[{ required: true }]}>
          <Select options={datasetOptions} showSearch disabled={isEdit} />
        </Form.Item>
        <Form.Item
          name="expr"
          label={t('fields.formula')}
          rules={[{ required: true, whitespace: true }]}
        >
          <FormulaEditor />
        </Form.Item>
        <Form.Item name="time_window" label="time_window">
          <Input placeholder="1d / 7d / 30d / mtd" />
        </Form.Item>
        <Form.Item name="additivity" label="additivity" rules={[{ required: true }]}>
          <Select options={METRIC_ADDITIVITY_OPTIONS} />
        </Form.Item>
        <Form.Item name="is_default" label="is_default" rules={[{ required: true }]}>
          <Select
            options={[
              { label: t('common.yes'), value: true },
              { label: t('common.no'), value: false },
            ]}
          />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionMetricFormulaModal;
