import { DIMENSION_TYPE, DIMENSION_PRIORITY_OPTIONS } from '@/constants';
import { Button, Drawer, Form, Input, Select, Space, message } from '@/design';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { createDimension, updateDimension } from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import type {
  DimensionCreateRequest,
  DimensionItem,
  DimensionPriority,
  DimensionUpdateRequest,
} from '@/types/dimensionManagement';
import { isFormValidationError } from '@/utils';
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface DimensionFormValues {
  domain_id: number;
  dimension_name: string;
  description?: string | null;
  synonyms?: string | null;
  dimension_type?: string | null;
  child_dimensions?: string[];
  priority: DimensionPriority;
}

interface ActionDimensionDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DimensionItem;
  initialFilter?: { domain_id?: number };
}

const ActionDimensionDrawer: React.FC<ActionDimensionDrawerProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
  initialFilter,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<DimensionFormValues>();
  const isEdit = record != null;
  const initialPriority: DimensionPriority =
    record?.priority?.trim().toLowerCase() === 'high' ? 'high' : 'normal';
  const [loading, setLoading] = useState(false);
  const [childOptions, setChildOptions] = useState<Array<{ label: string; value: string }>>([]);
  const { loadDomains, loadDimensionNames } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();

  const loadChildren = useCallback(
    async (domainId?: number) => {
      if (!domainId) {
        setChildOptions([]);
        return;
      }
      const options = await loadDimensionNames(domainId);
      setChildOptions(options.filter((option) => option.value !== record?.dimension_name));
    },
    [loadDimensionNames, record?.dimension_name],
  );

  useEffect(() => {
    if (!visible) return;
    const initialize = async () => {
      form.resetFields();
      form.setFieldsValue({ priority: 'normal' });
      if (record) {
        form.setFieldsValue({
          domain_id: record.domain_id,
          dimension_name: record.dimension_name,
          description: record.description,
          synonyms: record.synonyms,
          dimension_type: record.dimension_type,
          child_dimensions: record.child_dimensions?.split('$$$').filter(Boolean),
          priority: initialPriority,
        });
        await Promise.all([loadDomains(), loadChildren(record.domain_id)]);
        return;
      }
      await loadDomains();
      if (initialFilter?.domain_id) {
        form.setFieldValue('domain_id', initialFilter.domain_id);
        await loadChildren(initialFilter.domain_id);
      }
    };
    void initialize();
  }, [form, initialFilter, initialPriority, loadChildren, loadDomains, record, visible]);

  const handleDomainChange = async (domainId?: number) => {
    form.setFieldValue('child_dimensions', undefined);
    await loadChildren(domainId);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const productFields: DimensionUpdateRequest = {
        dimension_name: values.dimension_name,
        description: values.description || null,
        synonyms: values.synonyms || null,
        dimension_type: values.dimension_type || null,
        child_dimensions: values.child_dimensions?.join('$$$') || null,
        priority: !isEdit || values.priority !== initialPriority ? values.priority : undefined,
      };
      setLoading(true);
      if (record) {
        await updateDimension(record.dimension_id, productFields);
      } else {
        const createRequest: DimensionCreateRequest = {
          domain_id: values.domain_id,
          ...productFields,
          dimension_name: values.dimension_name,
        };
        await createDimension(createRequest);
      }
      message.success(record ? t('common.editSuccess') : t('common.createSuccess'));
      callback?.();
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
        <Form.Item name="domain_id" label={t('fields.businessDomain')} rules={[{ required: true }]}>
          <Select
            options={domainOptions}
            showSearch
            disabled={isEdit}
            onChange={(value) => void handleDomainChange(value)}
          />
        </Form.Item>
        <Form.Item
          name="dimension_name"
          label={t('fields.dimensionName')}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="description" label={t('fields.dimensionDescription')}>
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item name="synonyms" label={t('fields.synonyms')}>
          <Input />
        </Form.Item>
        <Form.Item name="dimension_type" label={t('fields.dimensionType')}>
          <Select options={DIMENSION_TYPE} allowClear />
        </Form.Item>
        <Form.Item name="child_dimensions" label="child_dimensions">
          <Select mode="tags" options={childOptions} tokenSeparators={[',']} />
        </Form.Item>
        <Form.Item name="priority" label="priority" rules={[{ required: true }]}>
          <Select options={DIMENSION_PRIORITY_OPTIONS} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionDimensionDrawer;
