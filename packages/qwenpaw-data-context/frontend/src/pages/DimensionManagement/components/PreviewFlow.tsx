import { Button, Space, Spin, message } from '@/design';
import { EditableProTable } from '@/design';
import type { ProColumns } from '@/design';
import { confirmDimensionStorage, previewDimension } from '@/services/dimensionManagement';
import type { DimensionCaliberResponse } from '@/types/dimensionManagement';
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

export interface PreviewFlowProps {
  datasetIds: number[];
  onBack: () => void;
}

const PreviewFlow: React.FC<PreviewFlowProps> = ({ datasetIds, onBack }) => {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [rows, setRows] = useState<DimensionCaliberResponse[]>([]);
  const [editableKeys, setEditableKeys] = useState<React.Key[]>([]);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const result = await previewDimension(datasetIds);
        setRows(result);
        setEditableKeys(result.map((item) => item.id));
      } finally {
        setLoading(false);
      }
    };
    void load();
  }, [datasetIds]);

  const columns: ProColumns<DimensionCaliberResponse>[] = [
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', editable: false },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', editable: false },
    { title: t('fields.calculateExpression'), dataIndex: 'expr' },
    { title: 'value_example', dataIndex: 'value_example' },
  ];

  const save = async () => {
    setSaving(true);
    try {
      await confirmDimensionStorage(rows);
      message.success(t('dimensionPreview.storageSuccess'));
      onBack();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Spin spinning={loading}>
      <EditableProTable<DimensionCaliberResponse>
        rowKey="id"
        columns={columns}
        value={rows}
        onChange={(value) => setRows([...value])}
        recordCreatorProps={false}
        editable={{ type: 'multiple', editableKeys, onChange: setEditableKeys }}
      />
      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Space>
          <Button onClick={onBack}>{t('dimensionPreview.backToTable')}</Button>
          <Button type="primary" loading={saving} onClick={() => void save()}>
            {t('dimensionValue.confirmStorage')}
          </Button>
        </Space>
      </div>
    </Spin>
  );
};

export default PreviewFlow;
