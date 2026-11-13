import React, { useEffect, useRef } from 'react';
import { getUiBridge } from '../../../embedded/bridge';
import { useHasCapability } from '../../../store/authStore';
import { Drawer, Form, Input, Button, Space, message, Select } from '@/design';
import { createDatasetMeta, updateDatasetMeta } from '@/services/datasetManagement';
import type { DatasetCreateRequest, DatasetManagementItem } from '@/types/datasetManagement';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import CodeMirrorSql from './CodeMirrorSql';
import { useTranslation } from 'react-i18next';
import { isFormValidationError } from '@/utils';

interface CreateOrEditDataMetaDrawerProps {
  onUiReady?: () => void; // Fired after record initialization and handler registration.
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
  onUiReady,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!record;
  const canWrite = useHasCapability('datasource:write');
  const manualEdit = useRef(false);
  const dirty = useRef(false);
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

  useEffect(() => {
    const bridge = getUiBridge();
    manualEdit.current = false;
    dirty.current = false;
    return () => bridge?.dirty('dataset.editor', false);
  }, [visible, record]);

  useEffect(() => {
    const bridge = getUiBridge();
    if (!bridge || !visible || !record || !canWrite) return;
    manualEdit.current = false;
    dirty.current = false;
    const key = 'dataset.editor';
    const description = bridge.register(
      {
        element_id: 'dataset.editor.description',
        allowed_actions: ['fill', 'clear', 'focus'],
        value_type: 'text',
      },
      async (action, context) => {
        context.assertCurrent();
        if (action.type === 'focus') {
          form.getFieldInstance('description')?.focus();
          return { completed_steps: ['focused'] };
        }
        if (manualEdit.current) throw new Error('USER_EDIT_CONFLICT');
        const value = action.type === 'clear' ? '' : action.value;
        if (typeof value !== 'string') throw new Error('INVALID_ARGUMENT');
        form.setFieldValue('description', value);
        dirty.current = value !== (record.description ?? '');
        bridge.dirty(key, dirty.current);
        return {
          value: form.getFieldValue('description'),
          saved: false,
          completed_steps: ['draft_updated'],
        };
      },
    );
    const editor = bridge.register(
      { element_id: key, allowed_actions: ['set_state'] },
      async (action, context) => {
        context.assertCurrent();
        if (action.visible !== false) throw new Error('ACTION_NOT_ALLOWED');
        if (dirty.current || manualEdit.current) throw new Error('USER_EDIT_CONFLICT');
        onCancel();
        return { completed_steps: ['editor_closed'] };
      },
    );
    onUiReady?.();
    return () => {
      description();
      editor();
      bridge.dirty(key, false);
    };
  }, [visible, record, canWrite, form, onCancel, onUiReady]);

  const handleSubmit = async () => {
    try {
      const values = (await form.validateFields()) as DatasetCreateRequest;
      const saved = record
        ? await updateDatasetMeta(record.dataset_id, {
            dataset_name: values.dataset_name,
            description: values.description,
            source: values.source,
          })
        : await createDatasetMeta(values);
      if (isEdit && !saved.datasource_id?.trim()) {
        message.warning(t('dataset.savedWithoutDatasource'));
      } else {
        message.success(isEdit ? t('common.editSuccess') : t('common.createSuccess'));
      }
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
        <Form
          form={form}
          layout="vertical"
          onValuesChange={() => {
            manualEdit.current = true;
            dirty.current = true;
            getUiBridge()?.dirty('dataset.editor', true);
          }}
        >
          <Form.Item
            label={t('fields.dataSource')}
            name="datasource_id"
            rules={[{ required: !isEdit, message: t('validation.selectDataSource') }]}
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
            rules={[{ required: !isEdit, message: t('validation.selectBusinessDomain') }]}
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
