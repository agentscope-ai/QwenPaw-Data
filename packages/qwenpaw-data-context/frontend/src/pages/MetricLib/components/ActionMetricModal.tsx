import { SEMANTIC_PRIORITY_OPTIONS } from '@/constants';
import { Button, Drawer, Form, Input, Select, Space, message } from '@/design';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { createMetric, updateMetric } from '@/services/metricLib';
import { useBusinessDomainOptions } from '@/store';
import type { MetricCreateRequest, MetricLibItem, MetricUpdateRequest } from '@/types/metricLib';
import { isFormValidationError } from '@/utils';
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface ActionMetricModalProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback: () => void;
  record?: Partial<MetricLibItem>;
  initialFilter?: { domain_id?: number };
}

const ActionMetricModal: React.FC<ActionMetricModalProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
  initialFilter,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<MetricCreateRequest>();
  const isEdit = record?.metric_id != null;
  const [loading, setLoading] = useState(false);
  const { loadDomains } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();

  useEffect(() => {
    if (!visible) return;
    const initialize = async () => {
      form.resetFields();
      form.setFieldValue('priority', 'MEDIUM');
      await loadDomains();
      if (record) {
        form.setFieldsValue(record as MetricCreateRequest);
      } else if (initialFilter?.domain_id) {
        form.setFieldValue('domain_id', initialFilter.domain_id);
      }
    };
    void initialize();
  }, [form, initialFilter, loadDomains, record, visible]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        const update: MetricUpdateRequest = {
          metric_name: values.metric_name,
          description: values.description || null,
          synonyms: values.synonyms || null,
          priority: values.priority,
        };
        await updateMetric(record!.metric_id!, update);
      } else {
        await createMetric(values);
      }
      message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      onCancel();
      callback();
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
      width={620}
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
        <Form.Item name="domain_id" label={t('fields.businessDomain')} rules={[{ required: true }]}>
          <Select options={domainOptions} showSearch disabled={isEdit} />
        </Form.Item>
        <Form.Item
          name="metric_name"
          label={t('fields.metricName')}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="description" label={t('fields.description')}>
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item name="synonyms" label={t('fields.synonyms')}>
          <Input />
        </Form.Item>
        <Form.Item name="priority" label="priority" rules={[{ required: true }]}>
          <Select options={SEMANTIC_PRIORITY_OPTIONS} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionMetricModal;
