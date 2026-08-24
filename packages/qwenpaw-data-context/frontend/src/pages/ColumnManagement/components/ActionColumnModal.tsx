import React, { useEffect, useMemo, useState } from 'react';
import { Drawer, Form, Input, Select, Row, Col, Button, Space, message } from '@/design';
import type { ColumnItem } from '@/types/column';
import type { DatasetManagementItem } from '@/types/datasetManagement';
import { createColumn, updateColumn } from '@/services/column';
import { queryDatasetMeta } from '@/services/datasetManagement';
import { useDataSourceOptionsStore, useDataSourceOptions, useBusinessDomainOptionsStore, useBusinessDomainOptions } from '@/store';
import { normalizeListResponse } from '@/utils/listResponse';
import { DIMENSION_TYPE, DATA_TYPE_OPTIONS, COLUMN_TYPE_OPTIONS } from '@/constants';
import { useTranslation } from 'react-i18next';
import { translateOptions } from '@/i18n/options';
import { isFormValidationError, omitKeys } from '@/utils';

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
  const [form] = Form.useForm();
  const isEdit = !!record?.id;
  const [loading, setLoading] = useState(false);

  const dataSourceOptions = useDataSourceOptions();
  const [selectedDatasourceId, setSelectedDatasourceId] = useState('');
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId);
  const [datasetOptions, setDatasetOptions] = useState<{ label: string; value: number }[]>([]);
  const columnTypeOptions = useMemo(
    () => translateOptions(t, COLUMN_TYPE_OPTIONS, 'column.typeOptions'),
    [t],
  );
  const dimensionTypeOptions = useMemo(
    () => translateOptions(t, DIMENSION_TYPE, 'dimension.typeOptions'),
    [t],
  );

  // 加载数据源
  useEffect(() => {
    if (!visible) return;
    // 强制刷新：每次打开弹窗都重新请求数据源数据
    useDataSourceOptionsStore.getState().fetchOptions(true);
  }, [visible]);

  // 加载数据集
  useEffect(() => {
    if (!visible) return;
    const fetchDatasets = async () => {
      try {
        const res = await queryDatasetMeta({ page: 1, size: 500 });
        const list = normalizeListResponse<DatasetManagementItem>(res);
        setDatasetOptions(list.map((item) => ({ label: item.dataset_name, value: item.dataset_id })));
      } catch { /* 获取数据集失败，静默处理 */ }
    };
    fetchDatasets();
  }, [visible]);

  // 数据源变化 → 加载业务域 & 过滤数据集
  const handleDatasourceChange = async (dsId: string) => {
    setSelectedDatasourceId(dsId || '');
    form.setFieldValue('domain_id', undefined);
    form.setFieldValue('dataset_id', undefined);
    if (!dsId) return;
    await useBusinessDomainOptionsStore.getState().fetchOptions(dsId);
  };

  // 业务域变化 → 过滤数据集
  const handleDomainChange = async (domainId: number) => {
    form.setFieldValue('dataset_id', undefined);
    if (!domainId) return;
    try {
      const res = await queryDatasetMeta({ page: 1, size: 500 });
      const list = normalizeListResponse<DatasetManagementItem>(res);
      setDatasetOptions(list.map((item) => ({ label: item.dataset_name, value: item.dataset_id })));
    } catch { /* 获取数据集失败，静默处理 */ }
  };

  useEffect(() => {
    if (visible) {
      if (isEdit) {
        form.setFieldsValue(record);
        // 编辑模式只需加载业务域选项，不能调用 handleDatasourceChange（会清空 domain_id/dataset_id）
        if (record?.datasource_id) {
          setSelectedDatasourceId(record.datasource_id);
          useBusinessDomainOptionsStore.getState().fetchOptions(record.datasource_id);
        }
      } else {
        form.resetFields();
        setSelectedDatasourceId('');
      }
    }
  }, [visible, record, isEdit, form]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        const rest = omitKeys(values, ['datasource_id', 'domain_id', 'dataset_id']);
        await updateColumn(record!.id!, rest);
      } else {
        await createColumn({ ...values, dimension_type: null });
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

  return (
    <Drawer
      title={title}
      width={600}
      open={visible}
      onClose={() => { onCancel(); form.resetFields(); }}
      destroyOnClose
      footer={
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => { onCancel(); form.resetFields(); }}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={handleSubmit}>{t('common.confirm')}</Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        {/* 数据源 */}
        <Form.Item
          label={t('fields.dataSource')}
          name="datasource_id"
          rules={[{ required: true, message: t('validation.selectDataSource') }]}
        >
          <Select
            placeholder={t('validation.selectDataSource')}
            allowClear
            showSearch
            optionFilterProp="label"
            options={dataSourceOptions}
            disabled={isEdit}
            onChange={handleDatasourceChange}
          />
        </Form.Item>

        {/* 业务域 */}
        <Form.Item
          label={t('fields.businessDomain')}
          name="domain_id"
          rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}
        >
          <Select
            placeholder={t('validation.selectBusinessDomain')}
            allowClear
            showSearch
            optionFilterProp="label"
            options={domainOptions}
            disabled={isEdit}
            onChange={handleDomainChange}
          />
        </Form.Item>

        {/* 所属数据集 ID */}
        <Form.Item
          label={t('column.datasetId')}
          name="dataset_id"
          rules={[{ required: true, message: t('column.selectDatasetId') }]}
        >
          <Select
            placeholder={t('column.selectDatasetId')}
            allowClear
            showSearch
            optionFilterProp="label"
            options={datasetOptions}
            disabled={isEdit}
          />
        </Form.Item>

        {/* 列名 */}
        <Form.Item
          label={t('column.name')}
          name="column_name"
          rules={[{ required: true, message: t('column.inputName') }]}
        >
          <Input placeholder={t('column.inputName')} />
        </Form.Item>

        {/* 列中文名 & 数据类型 */}
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item label={t('column.cnName')} name="column_name_cn">
              <Input placeholder={t('column.inputCnName')} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label={t('fields.dataType')} name="data_type">
              <Select
                placeholder={t('validation.selectDataType')}
                allowClear
                options={DATA_TYPE_OPTIONS}
              />
            </Form.Item>
          </Col>
        </Row>

        {/* 列类型 & 维度类型 */}
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item label={t('column.columnType')} name="column_type">
              <Select placeholder={t('column.selectColumnType')} allowClear options={columnTypeOptions} />
            </Form.Item>
          </Col>
          {isEdit && (
            <Col span={12}>
              <Form.Item label={t('fields.dimensionType')} name="dimension_type">
                <Select placeholder={t('validation.selectDimensionType')} allowClear options={dimensionTypeOptions} />
              </Form.Item>
            </Col>
          )}
        </Row>

        {/* 列注释 */}
        <Form.Item
          label={t('column.comment')}
          name="column_comment"
          rules={[
            { required: true, whitespace: true, message: t('column.inputComment') },
          ]}
        >
          <Input.TextArea
            rows={3}
            maxLength={500}
            showCount
            placeholder={t('column.inputComment')}
          />
        </Form.Item>

        {/* 枚举值 & 枚举说明 */}
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item label={t('fields.enums')} name="column_enums" extra={t('column.enumsExtra')}>
              <Input.TextArea rows={2} placeholder={t('column.enumsPlaceholder')} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label={t('column.enumDescription')} name="column_enums_description">
              <Input.TextArea rows={2} placeholder={t('column.inputEnumDescription')} />
            </Form.Item>
          </Col>
        </Row>

        {/* 样本值 */}
        <Form.Item label={t('column.samples')} name="samples">
          <Input.TextArea
            rows={3}
            maxLength={500}
            showCount
            placeholder={t('column.inputSamples')}
          />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionColumnModal;
