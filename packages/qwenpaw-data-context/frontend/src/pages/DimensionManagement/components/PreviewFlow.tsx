import { useState, useEffect, useMemo } from 'react';
import { Spin, Alert, Button, Space, message } from '@/design';
import { EditableProTable, ProColumns } from '@/design';
import { RollbackOutlined, SaveOutlined } from '@/design';
import type { DimensionCaliberItem } from '@/types/dimensionManagement';
import { previewDimension, confirmDimensionStorage } from '@/services/dimensionManagement';
import { DATA_TYPE_OPTIONS, DIMENSION_TYPE_OPTIONS } from '../constant';
import { useTranslation } from 'react-i18next';
import { translateOptions } from '@/i18n/options';
import { normalizeListResponse } from '@/utils/listResponse';

export interface PreviewFlowProps {
  datasetIds: number[];
  onBack: () => void;
}

const PreviewFlow: React.FC<PreviewFlowProps> = ({ datasetIds, onBack }) => {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [previewData, setPreviewData] = useState<DimensionCaliberItem[]>([]);
  const [editableKeys, setEditableKeys] = useState<React.Key[]>([]);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const dimensionTypeOptions = useMemo(
    () => translateOptions(t, DIMENSION_TYPE_OPTIONS, 'dimension.previewTypeOptions'),
    [t],
  );

  // 获取预览数据
  useEffect(() => {
    const fetchPreviewData = async () => {
      setLoading(true);
      try {
        const result = await previewDimension(datasetIds);
        const data = normalizeListResponse<DimensionCaliberItem>(result);
        setPreviewData(data);
        // 设置所有行为可编辑
        setEditableKeys(data.map((item: DimensionCaliberItem, index: number) => item.id ?? index));
      } catch {
        // API 错误由响应拦截器统一提示
      } finally {
        setLoading(false);
      }
    };
    fetchPreviewData();
  }, [datasetIds]);

  // 确认入库
  const handleConfirmStorage = async () => {
    setConfirmLoading(true);
    try {
      await confirmDimensionStorage(previewData);
      message.success(t('dimensionPreview.storageSuccess'));
      onBack();
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setConfirmLoading(false);
    }
  };

  const previewColumns: ProColumns<DimensionCaliberItem>[] = [
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', width: 140 },
    { title: t('fields.calculateExpression'), dataIndex: 'calculate_expr', width: 180 },
    {
      title: t('fields.dimensionType'),
      dataIndex: 'dimension_type',
      width: 120,
      valueType: 'select',
      fieldProps: { options: dimensionTypeOptions },
    },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      width: 120,
      valueType: 'select',
      fieldProps: { options: DATA_TYPE_OPTIONS },
    },
    { title: t('fields.synonyms'), dataIndex: 'synonyms', width: 160 },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', width: 140, editable: false },
    { title: t('fields.businessDomain'), dataIndex: 'domain', width: 120 },
  ];

  return (
    <Spin spinning={loading}>
      <Alert
        message={t('dimensionPreview.title')}
        description={t('dimensionPreview.description')}
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
      />
      <EditableProTable<DimensionCaliberItem>
        rowKey={(record, index) => record.id ?? index ?? 0}
        columns={previewColumns}
        value={previewData}
        onChange={(value) => setPreviewData([...value])}
        recordCreatorProps={false}
        options={{ density: false }}
        editable={{
          type: 'multiple',
          editableKeys,
          onChange: setEditableKeys,
        }}
        scroll={{ x: 1000 }}
      />
      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Space>
          <Button icon={<RollbackOutlined />} onClick={onBack}>
            {t('dimensionPreview.backToTable')}
          </Button>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            onClick={handleConfirmStorage}
            loading={confirmLoading}
          >
            {t('dimensionValue.confirmStorage')}
          </Button>
        </Space>
      </div>
    </Spin>
  );
};

export default PreviewFlow;
