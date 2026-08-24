import React, { useCallback, useEffect } from 'react';
import { Drawer, Form, Input, Button, Space, message, Select } from '@/design';
import { createDatasetMeta, updateDatasetMeta } from '@/services/datasetManagement';
import { DatasetManagementItem } from '@/types/datasetManagement';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import CodeMirrorSql from './CodeMirrorSql';
import { useTranslation } from 'react-i18next';
import { omitKeys } from '@/utils';

interface CreateOrEditDataMetaDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DatasetManagementItem;
}
const CreateOrEditDataMetaDrawer: React.FC<CreateOrEditDataMetaDrawerProps> = ({ title, visible, onCancel, callback, record }) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!record;
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains } = useCascadeFilterOptions();
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');

  const handleDatasourceChange = useCallback(async (dsId: string) => {
    form.setFieldValue('domain_id', undefined);
    if (!dsId) return;
    await loadDomains(dsId);
  }, [form, loadDomains]);

  useEffect(() => {
    if (visible && record) {
      form.setFieldsValue(record);
      // 编辑模式只需加载业务域选项，不能调用 handleDatasourceChange（会清空 domain_id）
      if (record.datasource_id) {
        loadDomains(record.datasource_id);
      }
    } else if (visible && !record) {
      form.resetFields();
      // domainOptions 会自动清空（因为 datasource_id 变为 undefined）
    }
  }, [visible, record, form, loadDomains]);

  const handleSubmit = async () => {
    try {
      const values = {
        ...(await form.validateFields()),
        dataset_type: null,
      };
      if (isEdit) {
        // 编辑模式不传 datasource_id / domain_id
        const rest = omitKeys(values, ['datasource_id', 'domain_id']);
        await updateDatasetMeta(record!.dataset_id, rest);
      } else {
        await createDatasetMeta(values);
      }
      message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      callback?.();
      onCancel();
    } catch (error) {
      console.error('提交失败:', error);
      message.error(isEdit ? t('common.editFailed') : t('common.createFailed'));
    }
  };

  return <>
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
          <Button onClick={() => {
            onCancel();
            form.resetFields();
          }}>{t('common.cancel')}</Button>
          <Button type="primary" onClick={handleSubmit}>{t('common.submit')}</Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical">
        <Form.Item label={t('fields.dataSource')} name="datasource_id" rules={[{ required: true, message: t('validation.selectDataSource') }]}>
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
        <Form.Item label={t('fields.businessDomain')} name="domain_id" rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}>
          <Select
            placeholder={t('validation.selectBusinessDomain')}
            options={domainOptions}
            allowClear
            showSearch
            optionFilterProp="label"
            disabled={isEdit}
          />
        </Form.Item>
        <Form.Item label={t('fields.datasetName')} name="dataset_name" rules={[{ required: true, message: t('validation.datasetNameRequired') }]} extra={t('dataset.nameExtra')}>
          <Input placeholder={t('validation.inputDatasetName')} />
        </Form.Item>
        <Form.Item label={t('fields.datasetDescription')} name="dataset_comment" rules={[{ required: true, message: t('validation.datasetDescriptionRequired') }]}>
          <Input.TextArea placeholder={t('validation.inputDatasetDescription')} />
        </Form.Item>
        <Form.Item label={t('fields.parentDataset')} name="parents">
          <Input placeholder={t('validation.inputParentDataset')} />
        </Form.Item>
        <Form.Item label={t('fields.sqlContent')} name="sql_content">
          <CodeMirrorSql />
        </Form.Item>
      </Form>
    </Drawer>
  </>;
};

export default CreateOrEditDataMetaDrawer;
