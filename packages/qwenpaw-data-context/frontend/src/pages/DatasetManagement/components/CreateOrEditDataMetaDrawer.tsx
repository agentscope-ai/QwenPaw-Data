import React, { useEffect } from 'react';
import { Drawer, Form, Input, Button, Space, message, Select } from '@/design';
import { createDatasetMeta, updateDatasetMeta } from '@/services/datasetManagement';
import type { DatasetCreateRequest, DatasetManagementItem } from '@/types/datasetManagement';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import CodeMirrorSql from './CodeMirrorSql';
import { useTranslation } from 'react-i18next';
import { isFormValidationError } from '@/utils';

interface CreateOrEditDataMetaDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DatasetManagementItem;
}
const CreateOrEditDataMetaDrawer: React.FC<CreateOrEditDataMetaDrawerProps> = ({
  title,
  visible,
  onCancel,
  callback,
  record,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!record;
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains } = useCascadeFilterOptions();
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');

  const handleDatasourceChange = async (dsId: string) => {
    if (!dsId) return;
    await loadDomains(dsId);
  };

  useEffect(() => {
    if (visible && record) {
      form.setFieldsValue(record);
      // 业务域独立于数据源，编辑保留原绑定。
      if (record.datasource_id) {
        loadDomains(record.datasource_id);
      }
    } else if (visible && !record) {
      form.resetFields();
      // 新建仍可选择工作空间内的全部业务域。
    }
  }, [visible, record, form, loadDomains]);

  const handleSubmit = async () => {
    try {
      const values = (await form.validateFields()) as DatasetCreateRequest;
      if (isEdit) {
        await updateDatasetMeta(record!.dataset_id, {
          dataset_name: values.dataset_name,
          description: values.description,
          source: values.source,
        });
      } else {
        await createDatasetMeta(values);
      }
      message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      callback?.();
      onCancel();
    } catch (error: unknown) {
      if (isFormValidationError(error)) return;
    }
  };

  return (
    <>
      <Drawer
        width={600}
        title={title}
        open={visible}
        destroyOnHidden
        onClose={() => {
          onCancel();
          form.resetFields();
        }}
        extra={
          <Space>
            <Button
              onClick={() => {
                onCancel();
                form.resetFields();
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button type="primary" onClick={handleSubmit}>
              {t('common.submit')}
            </Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item
            label={t('fields.dataSource')}
            name="datasource_id"
            rules={[{ required: true, message: t('validation.selectDataSource') }]}
          >
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
          <Form.Item
            label={t('fields.businessDomain')}
            name="domain_id"
            rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}
          >
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
            label={t('fields.datasetName')}
            name="dataset_name"
            rules={[{ required: true, message: t('validation.datasetNameRequired') }]}
            extra={t('dataset.nameExtra')}
          >
            <Input placeholder={t('validation.inputDatasetName')} />
          </Form.Item>
          <Form.Item
            label={t('fields.datasetDescription')}
            name="description"
            rules={[{ required: true, message: t('validation.datasetDescriptionRequired') }]}
          >
            <Input.TextArea placeholder={t('validation.inputDatasetDescription')} />
          </Form.Item>
          <Form.Item
            label={t('fields.sqlContent')}
            name="source"
            rules={[{ required: true, message: t('validation.fieldRequired') }]}
          >
            <CodeMirrorSql />
          </Form.Item>
        </Form>
      </Drawer>
    </>
  );
};

export default CreateOrEditDataMetaDrawer;
