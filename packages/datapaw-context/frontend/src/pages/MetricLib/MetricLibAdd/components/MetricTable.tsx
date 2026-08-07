import {
  CloseOutlined,
  InfoCircleOutlined,
} from '@/design';
import type { ActionType, EditableFormInstance, ProColumns } from '@/design';
import { EditableProTable } from '@/design';
import { Button, Space, Typography } from '@/design';
import React, { useRef } from 'react';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

// 扩展的指标数据类型（用于第二步口径录入）
export interface MetricLibItem {
  id: string;
  metricName: string;
  domain: string;
  synonyms: string;
  formula?: string;
  dateRange?: string;
  dataset?: string;
  formulaEvidence?: string;
  derivedFrom?: string;
  evidenceExt?: string;
  dataset_name?: string;
  dataset_id?: string;
}

type TableMode = 'simple' | 'full';

interface MetricTableProps {
  dataSource: readonly MetricLibItem[];
  editableKeys: React.Key[];
  onDataChange: (data: MetricLibItem[]) => void;
  onEditableKeysChange: (keys: React.Key[]) => void;
  onDeleteRow: (id: string) => void;
  mode?: TableMode;
}

const MetricTable: React.FC<MetricTableProps> = ({
  dataSource,
  editableKeys,
  onDataChange,
  onEditableKeysChange,
  onDeleteRow,
  mode = 'simple',
}) => {
  const { t } = useTranslation();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const editableFormRef = useRef<EditableFormInstance<MetricLibItem> | undefined>(undefined);

  // 基础列定义（简单模式）
  const getSimpleColumns = (): ProColumns<MetricLibItem>[] => [
    {
      title: t('metricAdd.index'),
      dataIndex: 'index',
      valueType: 'indexBorder',
      width: 60,
      editable: false,
    },
    {
      title: t('fields.metricName'),
      dataIndex: 'metricName',
      formItemProps: {
        rules: [{ required: true, message: t('validation.inputMetricName') }],
      },
      fieldProps: {
        placeholder: t('validation.inputMetricName'),
      },
    },
    {
      title: t('metricAdd.domain'),
      dataIndex: 'domain',
      formItemProps: {
        rules: [{ required: true, message: t('metricAdd.inputDomain') }],
      },
      fieldProps: {
        placeholder: t('metricAdd.inputDomain'),
      },
    },
    {
      title: t('fields.synonyms'),
      dataIndex: 'synonyms',
      fieldProps: {
        placeholder: t('metric.synonymsPlaceholder'),
      },
    },
  ];

  // 完整列定义（口径录入模式）
  const getFullColumns = (): ProColumns<MetricLibItem>[] => [
    {
      title: t('metricAdd.index'),
      dataIndex: 'index',
      valueType: 'indexBorder',
      width: 60,
      editable: false,
      fixed: 'left',
    },
    {
      title: t('fields.metricName'),
      dataIndex: 'metricName',
      width: 120,
      formItemProps: {
        rules: [{ required: true, message: t('validation.inputMetricName') }],
      },
      fieldProps: {
        placeholder: t('validation.inputMetricName'),
      },
    },
    {
      title: t('metricAdd.domain'),
      dataIndex: 'domain',
      width: 120,
      formItemProps: {
        rules: [{ required: true, message: t('metricAdd.inputDomain') }],
      },
      fieldProps: {
        placeholder: t('metricAdd.inputDomain'),
      },
    },
    {
      title: t('metricAdd.calculationFormula'),
      dataIndex: 'formula',
      width: 120,
      fieldProps: {
        placeholder: t('metricAdd.inputCalculationFormula'),
      },
    },
    {
      title: t('fields.timeRange'),
      dataIndex: 'dateRange',
      width: 120,
      fieldProps: {
        placeholder: t('validation.inputTimeRange'),
      },
    },
    {
      title: t('metricAdd.calculationEvidence'),
      dataIndex: 'formulaEvidence',
      width: 120,
      fieldProps: {
        placeholder: t('metricAdd.inputCalculationEvidence'),
      },
    },
    {
      title: t('fields.derivedFrom'),
      dataIndex: 'derivedFrom',
      width: 120,
      fieldProps: {
        placeholder: t('validation.inputDerivedFrom'),
      },
    },
    {
      title: t('metricAdd.extendedEvidence'),
      dataIndex: 'evidenceExt',
      width: 120,
      fieldProps: {
        placeholder: t('metricAdd.inputExtendedEvidence'),
      },
    },
  ];

  // 操作列
  const getActionColumn = (): ProColumns<MetricLibItem> => ({
    title: t('common.actions'),
    valueType: 'option',
    width: 140,
    fixed: 'right',
    render: (_, record, __, action) => {
      const isEditing = editableKeys.includes(record.id);
      if (isEditing) {
        return (
          <Space size="small">
            <Button
              type="link"
              size="small"
              onClick={() => {
                action?.saveEditable?.(record.id);
              }}
            >{t('common.save')}</Button>
            <Button
              type="link"
              size="small"
              icon={<CloseOutlined />}
              onClick={() => {
                action?.cancelEditable?.(record.id);
              }}
            />
            <Button
              type="link"
              danger
              size="small"
              onClick={() => onDeleteRow(record.id)}
            >{t('common.delete')}</Button>
          </Space>
        );
      }
      return (
        <Space size="small">
          <Button
            type="link"
            size="small"
            onClick={() => {
              action?.startEditable?.(record.id);
              onEditableKeysChange([record.id]);
            }}
           >{t('common.edit')}</Button>
          <Button
            type="link"
            danger
            size="small"
            onClick={() => onDeleteRow(record.id)}
           >{t('common.delete')}</Button>
        </Space>
      );
    },
  });

  // 根据模式获取列定义
  const columns: ProColumns<MetricLibItem>[] = [
    ...(mode === 'simple' ? getSimpleColumns() : getFullColumns()),
    getActionColumn(),
  ];

  return (
    <EditableProTable<MetricLibItem>
      headerTitle={
        <Space>
          <span>{t('metricAdd.metricList')}</span>
          <Text type="secondary">{t('common.total', { count: dataSource.length })}</Text>
        </Space>
      }
      actionRef={actionRef}
      editableFormRef={editableFormRef}
      columns={columns}
      value={dataSource}
      onChange={(value) => onDataChange(value as MetricLibItem[])}
      rowKey="id"
      search={false}
      scroll={{ x: 1100 }}
      options={{
        density: false,
        search: {
          placeholder: t('metricAdd.searchList'),
        },
      }}
      pagination={false}
      bordered
      size="small"
      recordCreatorProps={false}
      editable={{
        type: 'multiple',
        editableKeys,
        onChange: onEditableKeysChange,
        onValuesChange: (_record, recordList) => {
          onDataChange(recordList as MetricLibItem[]);
        },
      }}
      locale={{
        emptyText: (
          <div style={{ padding: 40, textAlign: 'center' }}>
            <InfoCircleOutlined style={{ fontSize: 48, color: '#d9d9d9' }} />
            <p style={{ marginTop: 16, color: '#999' }}>
              {t('metricAdd.emptyTableHint')}
            </p>
          </div>
        ),
      }}
    />
  );
};

export default MetricTable;
