import React, { useCallback, useEffect, useState } from 'react';
import { AccessibleFormLabel, Drawer, Form, Input, Select, Button, Space, message } from '@/design';
import type { MetricLibItem } from '@/types/metricLib';
import { createMetric, updateMetric } from '@/services/metricLib';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { useTranslation } from 'react-i18next';
import { isFormValidationError, omitKeys } from '@/utils';

interface ActionMetricModalProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback: () => void;
  record?: Partial<MetricLibItem>;
  initialFilter?: {
    datasource_id?: string;
    domain_id?: number;
  };
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
  const [form] = Form.useForm();
  const isEdit = !!record?.id;
  const [loading, setLoading] = useState(false);
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains } = useCascadeFilterOptions();
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');
  const yesNoOptions = [
    { label: t('common.yes'), value: true },
    { label: t('common.no'), value: false },
  ];

  const handleDatasourceChange = useCallback(async (dsId: string) => {
    form.setFieldValue('domain_id', undefined);
    if (!dsId) return;
    await loadDomains(dsId);
  }, [form, loadDomains]);

  useEffect(() => {
    if (visible) {
      if (isEdit) {
        form.setFieldsValue(record);
        // 编辑模式只需加载业务域选项，不能调用 handleDatasourceChange（会清空 domain_id）
        if (record?.datasource_id) {
          loadDomains(record.datasource_id);
        }
      } else {
        form.resetFields();
        // domainOptions 会自动清空（因为 datasource_id 变为 undefined）
        // 新增模式：带入搜索栏的数据源/业务域
        if (initialFilter?.datasource_id) {
          form.setFieldValue('datasource_id', initialFilter.datasource_id);
          handleDatasourceChange(initialFilter.datasource_id).then(() => {
            if (initialFilter.domain_id) {
              form.setFieldValue('domain_id', initialFilter.domain_id);
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
    loadDomains,
  ]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        // 编辑模式不传绑定关系字段
        const rest = omitKeys(values, ['datasource_id', 'domain_id']);
        await updateMetric(record!.id!, rest);
      } else {
        await createMetric(values);
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
      width={600}
      destroyOnClose
      footer={
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={handleClose}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={handleSubmit}>{t('common.confirm')}</Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        <Form.Item name="datasource_id" label={t('fields.dataSource')} rules={[{ required: true, message: t('validation.selectDataSource') }]}>
          <Select
            placeholder={t('validation.selectDataSource')}
            options={dataSourceOptions}
            allowClear
            showSearch
            optionFilterProp="label"
            disabled={isEdit}
            onChange={handleDatasourceChange}
          />
        </Form.Item>
        <Form.Item name="domain_id" label={t('fields.businessDomain')} rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}>
          <Select
            placeholder={t('validation.selectBusinessDomain')}
            options={domainOptions}
            allowClear
            showSearch
            optionFilterProp="label"
            disabled={isEdit}
          />
        </Form.Item>
        <Form.Item
          label={t('fields.metricName')}
          name="metric_name"
          rules={[{ required: true, message: t('validation.inputMetricName') }]}
        >
          <Input placeholder={t('validation.inputMetricName')} disabled={isEdit} />
        </Form.Item>
        <Form.Item label={t('fields.description')} name="description">
          <Input.TextArea rows={3} placeholder={t('metric.descriptionPlaceholder')} />
        </Form.Item>
        <Form.Item label={t('fields.unit')} name="unit">
          <Input placeholder={t('metric.unitPlaceholder')} />
        </Form.Item>
        <Form.Item
          name="is_polaris"
          label={
            <AccessibleFormLabel
              label={t('fields.isPolaris')}
              description={t('metric.isPolarisDescription')}
            />
          }
        >
          <Select placeholder={t('common.selectPlaceholder')} options={yesNoOptions} allowClear />
        </Form.Item>
        <Form.Item
          name="show_distribution"
          label={
            <AccessibleFormLabel
              label={t('fields.showDistribution')}
              description={t('metric.showDistributionDescription')}
            />
          }
        >
          <Select placeholder={t('common.selectPlaceholder')} options={yesNoOptions} allowClear />
        </Form.Item>
        <Form.Item
          name="is_visible"
          label={
            <AccessibleFormLabel
              label={t('fields.isVisible')}
              description={t('metric.isVisibleDescription')}
            />
          }
        >
          <Select placeholder={t('common.selectPlaceholder')} options={yesNoOptions} allowClear />
        </Form.Item>
        <Form.Item label={t('fields.synonyms')} name="synonyms">
          <Input placeholder={t('metric.synonymsPlaceholder')} />
        </Form.Item>
        <Form.Item label={t('fields.tags')} name="tags">
          <Input placeholder={t('metric.tagsPlaceholder')} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionMetricModal;
