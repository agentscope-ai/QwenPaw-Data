import React, { useCallback, useEffect, useState } from 'react';
import { AccessibleFormLabel, Drawer, Form, Input, Button, Space, message, Select, InputNumber, Radio, Row, Col } from '@/design';
import { createDimension, updateDimension } from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import { useDataSourceFilterOptions, useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { formatDatasourceLabel, isFormValidationError, omitKeys } from '@/utils';
import type { DimensionItem } from '@/types/dimensionManagement';
import { useTranslation } from 'react-i18next';

interface ActionDimensionDrawerProps {
  title: string;
  visible: boolean;
  onCancel: () => void;
  callback?: () => void;
  record?: DimensionItem;
  /** 新增时可从列表筛选条件预填数据源/业务域 */
  initialFilter?: {
    datasource_id?: string;
    domain_id?: number;
  };
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
  const [form] = Form.useForm();
  const isEdit = !!record?.id;
  const selectedDomainId = Form.useWatch('domain_id', form);
  const selectedDatasourceId = Form.useWatch('datasource_id', form);
  const parentSelectEnabled = isEdit ? !!record?.domain_id : !!selectedDomainId;
  const [loading, setLoading] = useState(false);
  const { options: dataSourceOptions } = useDataSourceFilterOptions();
  const { loadDomains, loadDimensionIds } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions(selectedDatasourceId || '');
  const [parentOptions, setParentOptions] = useState<{ label: string; value: string }[]>([]);

  const fetchDomainOptions = useCallback(async (dsId: string, resetFields = true) => {
    if (resetFields) {
      form.setFieldValue('domain_id', undefined);
      form.setFieldValue('parent_name', undefined);
    }
    if (!resetFields) {
      setParentOptions([]);
    }
    if (!dsId) return;
    await loadDomains(dsId);
  }, [form, loadDomains]);

  const loadParentOptions = useCallback(async (
    domainId: number,
    excludeId?: number,
    resetParent = true,
  ) => {
    if (resetParent) {
      form.setFieldValue('parent_name', undefined);
    }
    setParentOptions([]);
    if (!domainId) return;
    try {
      const options = await loadDimensionIds(domainId);
      setParentOptions(
        options
          .filter((item) => item.value !== excludeId)
          .map((item) => ({ label: item.label, value: item.label }))
      );
    } catch (error) {
      console.error('加载父维度选项失败:', error);
    }
  }, [form, loadDimensionIds]);

  useEffect(() => {
    if (!visible) return;

    if (record) {
      form.setFieldsValue({
        dimension_name: record.dimension_name,
        description: record.description,
        parent_name: record.parent_name || undefined,
        depth: record.depth,
        synonyms: record.synonyms,
        is_visible: record.is_visible ?? true,
        is_attribution: record.is_attribution ?? true,
        enums: record.enums,
      });
      if (record.domain_id) {
        loadParentOptions(record.domain_id, record.id, false);
      }
    } else {
      form.resetFields();
      // domainOptions 会自动清空（因为 datasource_id 变为 undefined）
      setParentOptions([]);
      form.setFieldsValue({
        is_visible: true,
        is_attribution: true,
      });

      if (initialFilter?.datasource_id) {
        form.setFieldValue('datasource_id', initialFilter.datasource_id);
        fetchDomainOptions(initialFilter.datasource_id, false).then(() => {
          if (initialFilter.domain_id) {
            form.setFieldValue('domain_id', initialFilter.domain_id);
            loadParentOptions(initialFilter.domain_id, undefined, false);
          }
        });
      }
    }
  }, [
    visible,
    record,
    initialFilter,
    form,
    fetchDomainOptions,
    loadParentOptions,
  ]);

  const handleDatasourceChange = (dsId: string) => {
    fetchDomainOptions(dsId, true);
  };

  const handleDomainChange = (domainId: number) => {
    loadParentOptions(domainId, record?.id, true);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      if (isEdit) {
        const rest = omitKeys(values, ['datasource_id', 'domain_id']);
        await updateDimension(record!.id, rest);
        message.success(t('common.editSuccess'));
      } else {
        await createDimension(values);
        message.success(t('common.createSuccess'));
      }
      callback?.();
      onCancel();
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
      width={720}
      destroyOnClose
      footer={
        <div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={handleClose}>{t('common.cancel')}</Button>
            <Button type="primary" loading={loading} onClick={handleSubmit}>
              {t('common.confirm')}
            </Button>
          </Space>
        </div>
      }
    >
      <Form form={form} layout="vertical">
        {isEdit ? (
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label={t('fields.dataSource')}>
                <Input value={formatDatasourceLabel(record?.datasource_name, record?.datasource_id)} disabled />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label={t('fields.businessDomain')}>
                <Input value={record?.domain_name || '-'} disabled />
              </Form.Item>
            </Col>
          </Row>
        ) : (
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="datasource_id"
                label={t('fields.dataSource')}
                rules={[{ required: true, message: t('validation.selectDataSource') }]}
                extra={t('dimension.dataSourceExtra')}
              >
                <Select
                  placeholder={t('validation.selectDataSource')}
                  options={dataSourceOptions}
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  onChange={handleDatasourceChange}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="domain_id"
                label={t('fields.businessDomain')}
                rules={[{ required: true, message: t('validation.selectBusinessDomain') }]}
                extra={t('dimension.businessDomainExtra')}
              >
                <Select
                  placeholder={t('validation.selectBusinessDomain')}
                  options={domainOptions}
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  onChange={handleDomainChange}
                />
              </Form.Item>
            </Col>
          </Row>
        )}

        <Form.Item
          name="dimension_name"
          label={t('fields.dimensionName')}
          rules={[{ required: true, message: t('dimension.inputDimensionName') }]}
        >
          <Input placeholder={t('dimension.inputDimensionName')} allowClear disabled={isEdit} />
        </Form.Item>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="description" label={t('fields.dimensionDescription')} extra={t('dimension.descriptionExtra')}>
              <Input.TextArea
                rows={4}
                placeholder={t('dimension.inputDimensionDescription')}
                maxLength={500}
                showCount
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="parent_name" label={t('fields.parentDimensionName')} extra={t('dimension.parentExtra')}>
              <Select
                placeholder={t('dimension.selectParentDimension')}
                options={parentOptions}
                allowClear
                showSearch
                optionFilterProp="label"
                disabled={!parentSelectEnabled}
              />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="depth" label={t('fields.depth')}>
              <InputNumber
                min={1}
                placeholder={t('dimension.inputDepth')}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="synonyms" label={t('fields.synonyms')} extra={t('dimension.synonymsExtra')}>
              <Input.TextArea
                rows={4}
                placeholder={t('dimension.synonymsPlaceholder')}
                maxLength={500}
                showCount
              />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="is_visible"
              label={
                <AccessibleFormLabel
                  label={t('fields.isVisible')}
                  description={t('dimension.isVisibleDescription')}
                />
              }
              rules={[{ required: true, message: t('dimension.selectVisible') }]}
            >
              <Radio.Group optionType="button" buttonStyle="solid">
                <Radio.Button value={true}>{t('common.yes')}</Radio.Button>
                <Radio.Button value={false}>{t('common.no')}</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="is_attribution"
              label={
                <AccessibleFormLabel
                  label={t('fields.isAttribution')}
                  description={t('dimension.isAttributionDescription')}
                />
              }
              rules={[{ required: true, message: t('dimension.selectAttribution') }]}
            >
              <Radio.Group optionType="button" buttonStyle="solid">
                <Radio.Button value={true}>{t('common.yes')}</Radio.Button>
                <Radio.Button value={false}>{t('common.no')}</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Col>
        </Row>

        <Form.Item
          name="enums"
          label={t('fields.enums')}
          extra={t('dimension.enumsExtra')}
        >
          <Input.TextArea
            rows={4}
            placeholder={t('dimension.inputEnums')}
            maxLength={1000}
            showCount
          />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

export default ActionDimensionDrawer;
