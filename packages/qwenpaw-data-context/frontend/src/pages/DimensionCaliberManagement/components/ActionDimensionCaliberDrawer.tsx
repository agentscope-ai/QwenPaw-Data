import { FullscreenOutlined } from '@/design';
import { Button, Col, Drawer, Form, Input, Modal, Row, Select, Space, message } from '@/design';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { createDimensionCaliber, updateDimensionCaliber } from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import type {
  DimensionCaliberCreateRequest,
  DimensionCaliberItem,
} from '@/types/dimensionManagement';
import { isFormValidationError } from '@/utils';
import { githubLight } from '@uiw/codemirror-theme-github';
import CodeMirror from '@uiw/react-codemirror';
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface ActionDimensionCaliberDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DimensionCaliberItem;
  datasourceId?: string;
  initialFilter?: {
    domain_id?: number;
    dimension_name?: string;
    dataset_id?: number;
  };
}

const ExprEditor: React.FC<{
  value?: string;
  onChange?: (value: string) => void;
}> = ({ value, onChange }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const editor = (height: string) => (
    <CodeMirror
      value={value ?? ''}
      height={height}
      theme={githubLight}
      onChange={(next) => onChange?.(next)}
      placeholder={t('validation.inputCalculateExpression')}
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

const ActionDimensionCaliberDrawer: React.FC<ActionDimensionCaliberDrawerProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
  initialFilter,
  datasourceId,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<DimensionCaliberCreateRequest & { domain_id: number }>();
  const isEdit = record != null;
  const [loading, setLoading] = useState(false);
  const { loadDomains, loadDatasets, loadDimensionIds } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();
  const [datasetOptions, setDatasetOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [dimensionOptions, setDimensionOptions] = useState<Array<{ label: string; value: number }>>(
    [],
  );

  const loadChildren = useCallback(
    async (domainId?: number) => {
      if (!domainId) {
        setDatasetOptions([]);
        setDimensionOptions([]);
        return;
      }
      const [datasets, dimensions] = await Promise.all([
        loadDatasets(domainId, datasourceId),
        loadDimensionIds(domainId),
      ]);
      setDatasetOptions(datasets);
      setDimensionOptions(dimensions);
      return dimensions;
    },
    [datasourceId, loadDatasets, loadDimensionIds],
  );

  useEffect(() => {
    if (!visible) return;
    const initialize = async () => {
      form.resetFields();
      const domainId = record?.domain_id ?? initialFilter?.domain_id;
      await loadDomains();
      let dimensions: Array<{ label: string; value: number }> = [];
      if (domainId) {
        form.setFieldValue('domain_id', domainId);
        dimensions = (await loadChildren(domainId)) ?? [];
      }
      if (record) {
        form.setFieldsValue(record);
      } else {
        if (initialFilter?.dataset_id) form.setFieldValue('dataset_id', initialFilter.dataset_id);
        if (initialFilter?.dimension_name) {
          const dimension = dimensions.find((item) => item.label === initialFilter.dimension_name);
          if (dimension) form.setFieldValue('dimension_id', dimension.value);
        }
      }
    };
    void initialize();
  }, [form, initialFilter, loadChildren, loadDomains, record, visible]);

  const handleDomainChange = async (domainId?: number) => {
    form.setFieldsValue({ dataset_id: undefined, dimension_id: undefined });
    await loadChildren(domainId);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (record) {
        await updateDimensionCaliber(record.id, {
          expr: values.expr,
          value_example: values.value_example || null,
        });
      } else {
        await createDimensionCaliber(values);
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
        <Form.Item name="dataset_id" label={t('fields.datasetName')} rules={[{ required: true }]}>
          <Select options={datasetOptions} showSearch disabled={isEdit} />
        </Form.Item>
        <Form.Item
          name="dimension_id"
          label={t('fields.dimensionName')}
          rules={[{ required: true }]}
        >
          <Select options={dimensionOptions} showSearch disabled={isEdit} />
        </Form.Item>
        <Form.Item
          name="expr"
          label={t('fields.calculateExpression')}
          rules={[{ required: true, whitespace: true }]}
        >
          <ExprEditor />
        </Form.Item>
        <Form.Item name="value_example" label="value_example">
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionDimensionCaliberDrawer;
