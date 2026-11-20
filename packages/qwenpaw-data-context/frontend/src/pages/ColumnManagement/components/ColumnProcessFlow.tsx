import { DATA_TYPE_OPTIONS, SEMANTIC_YES_NO_OPTIONS } from '@/constants';
import { Alert, Button, Space, Spin, message } from '@/design';
import { EditableProTable } from '@/design';
import type { ProColumns } from '@/design';
import { confirmColumnMetaStorage, previewColumnMeta } from '@/services/column';
import type { ColumnUpsertInput } from '@/types/column';
import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface ColumnProcessFlowProps {
  datasetId: number;
}

const ColumnProcessFlow: React.FC<ColumnProcessFlowProps> = ({ datasetId }) => {
  const { t } = useTranslation();
  const [rows, setRows] = useState<ColumnUpsertInput[]>([]);
  const [editableKeys, setEditableKeys] = useState<React.Key[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await previewColumnMeta(datasetId);
      setRows(result);
      setEditableKeys(result.map((item) => item.id));
    } finally {
      setLoading(false);
    }
  }, [datasetId]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ProColumns<ColumnUpsertInput>[] = [
    { title: t('column.name'), dataIndex: 'field_name', width: 180 },
    { title: t('fields.description'), dataIndex: 'description', width: 220 },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      valueType: 'select',
      fieldProps: { options: DATA_TYPE_OPTIONS },
      width: 140,
    },
    {
      title: 'is_primary',
      dataIndex: 'is_primary',
      valueType: 'select',
      fieldProps: { options: SEMANTIC_YES_NO_OPTIONS },
      width: 120,
    },
    {
      title: 'is_nullable',
      dataIndex: 'is_nullable',
      valueType: 'select',
      fieldProps: { options: SEMANTIC_YES_NO_OPTIONS },
      width: 120,
    },
  ];

  const save = async () => {
    setSaving(true);
    try {
      await confirmColumnMetaStorage(datasetId, rows);
      message.success(t('common.saveSuccess'));
      await load();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Spin spinning={loading}>
      <Alert type="info" showIcon message={t('column.previewTitle')} style={{ marginBottom: 16 }} />
      <EditableProTable<ColumnUpsertInput>
        rowKey={(record, index) => record.id ?? index ?? 0}
        columns={columns}
        value={rows}
        onChange={(value) => setRows([...value])}
        recordCreatorProps={false}
        editable={{ type: 'multiple', editableKeys, onChange: setEditableKeys }}
      />
      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Space>
          <Button onClick={() => void load()}>{t('common.refresh')}</Button>
          <Button type="primary" loading={saving} onClick={() => void save()}>
            {t('common.save')}
          </Button>
        </Space>
      </div>
    </Spin>
  );
};

export default ColumnProcessFlow;
