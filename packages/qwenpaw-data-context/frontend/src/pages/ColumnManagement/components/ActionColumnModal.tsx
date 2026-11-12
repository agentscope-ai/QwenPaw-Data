import { DATA_TYPE_OPTIONS, SEMANTIC_YES_NO_OPTIONS } from '@/constants';
import { Button, Drawer, Form, Input, Select, Space, message } from '@/design';
import { createColumn, updateColumn } from '@/services/column';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { useBusinessDomainOptions, useDataSourceOptions, useDataSourceOptionsStore } from '@/store';
import type { ColumnCreateRequest, ColumnItem, ColumnUpdateRequest } from '@/types/column';
import { isFormValidationError } from '@/utils';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface ActionColumnModalProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback: () => void;
  record?: Partial<ColumnItem>;
}

const ActionColumnModal: React.FC<ActionColumnModalProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm<ColumnCreateRequest>();
  const isEdit = record?.id != null;
  const [loading, setLoading] = useState(false);
  const selectedDatasourceId = Form.useWatch('datasource_id', form) ?? '';
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId);
  const dataSourceOptions = useDataSourceOptions();
  const [datasetOptions, setDatasetOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const datasetRequestId = useRef(0);

  const loadDatasets = useCallback(async (datasourceId?: string, domainId?: number) => {
    const requestId = ++datasetRequestId.current;
    setDatasetOptions([]);
    setDatasetsLoading(true);
    try {
      const options: Array<{ label: string; value: number }> = [];
      let page = 1;
      let total = 0;
      do {
        const result = await queryDatasetMeta({
          datasource_id: datasourceId || undefined,
          domain_id: domainId,
          page,
          size: 500,
        });
        if (requestId !== datasetRequestId.current) return;
        total = result.total;
        options.push(
          ...result.records.map((item) => ({
            label: item.dataset_name,
            value: item.dataset_id,
          })),
        );
        if (!result.records.length) break;
        page += 1;
      } while (options.length < total);
      setDatasetOptions(options);
    } catch {
      // API errors are displayed by the shared request interceptor.
    } finally {
      if (requestId === datasetRequestId.current) setDatasetsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void useDataSourceOptionsStore.getState().fetchOptions(true);
    form.resetFields();
    if (isEdit) {
      form.setFieldsValue(record as ColumnCreateRequest);
    } else {
      form.setFieldsValue({
        dataset_id: record?.dataset_id,
        is_primary: 'N',
        is_nullable: 'Y',
      } as ColumnCreateRequest);
    }
    void loadDatasets(form.getFieldValue('datasource_id'), form.getFieldValue('domain_id'));
    return () => {
      datasetRequestId.current += 1;
    };
  }, [form, isEdit, loadDatasets, record, visible]);

  const handleDatasourceChange = async (datasourceId?: string) => {
    form.setFieldValue('dataset_id', undefined);
    await loadDatasets(datasourceId, form.getFieldValue('domain_id'));
  };

  const handleDomainChange = async (domainId?: number) => {
    form.setFieldValue('dataset_id', undefined);
    await loadDatasets(form.getFieldValue('datasource_id'), domainId);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        const update: ColumnUpdateRequest = {
          field_name: values.field_name,
          description: values.description,
          data_type: values.data_type,
          is_primary: values.is_primary,
          is_nullable: values.is_nullable,
        };
        await updateColumn(record!.id!, update);
      } else {
        await createColumn(values);
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

  const handleClose = () => {
    form.resetFields();
    onCancel();
  };

  return (
    <Drawer
      title={title}
      width={640}
      open={visible}
      onClose={handleClose}
      destroyOnClose
      footer={
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={handleClose}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={() => void handleSubmit()}>
              {t('common.confirm')}
            </Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        <Form.Item name="datasource_id" label={t('fields.dataSource')} rules={[{ required: true }]}>
          <Select
            options={dataSourceOptions}
            showSearch
            allowClear={!isEdit}
            disabled={isEdit}
            onChange={(value) => void handleDatasourceChange(value)}
          />
        </Form.Item>
        <Form.Item name="domain_id" label={t('fields.businessDomain')} rules={[{ required: true }]}>
          <Select
            options={domainOptions}
            showSearch
            allowClear={!isEdit}
            disabled={isEdit}
            onChange={(value) => void handleDomainChange(value)}
          />
        </Form.Item>
        <Form.Item name="dataset_id" label={t('fields.datasetName')} rules={[{ required: true }]}>
          <Select
            options={datasetOptions}
            showSearch
            optionFilterProp="label"
            loading={datasetsLoading}
            disabled={isEdit || datasetsLoading}
          />
        </Form.Item>
        <Form.Item
          name="field_name"
          label={t('column.name')}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="description" label={t('fields.description')}>
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item name="data_type" label={t('fields.dataType')} rules={[{ required: true }]}>
          <Select options={DATA_TYPE_OPTIONS} showSearch />
        </Form.Item>
        <Form.Item name="is_primary" label="is_primary" rules={[{ required: true }]}>
          <Select options={SEMANTIC_YES_NO_OPTIONS} />
        </Form.Item>
        <Form.Item name="is_nullable" label="is_nullable" rules={[{ required: true }]}>
          <Select options={SEMANTIC_YES_NO_OPTIONS} />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionColumnModal;
