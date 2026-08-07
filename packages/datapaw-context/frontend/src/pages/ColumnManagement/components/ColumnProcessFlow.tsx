import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Steps, Button, Space, message, Spin, Alert, Tag, Card, Typography, Avatar } from '@/design';
import { EditableProTable } from '@/design';
import type { ProColumns, ActionType } from '@/design';
import { useNavigate } from 'react-router';
import {  UserOutlined } from '@/design';
import type { ColumnItem } from '@/types/column';
import {
  previewColumnMeta,
  confirmColumnMetaStorage,
  inferDimensions,
  confirmDimensions,
  completeSamples,
  confirmSamples,
} from '@/services/column';
import { DATA_TYPE_OPTIONS, DIMENSION_TYPE, STEPS } from '@/constants';
import { useTranslation } from 'react-i18next';
import { translateOptions, translateOptionValue } from '@/i18n/options';
import { normalizeListResponse } from '@/utils/listResponse';


const { Text, Title } = Typography;

function addEditableKeys(data: ColumnItem[]): ColumnItem[] {
  return data.map((item, index) => ({
    ...item,
    idx: item.id ?? `temp_${index}_${Date.now()}`,
  }));
}

function resolveColumnItems(result: unknown, fallback: ColumnItem[] = []): ColumnItem[] {
  const list = normalizeListResponse<ColumnItem>(result);
  if (list.length > 0 || Array.isArray(result)) return list;
  return fallback;
}

function hasBlankColumnComment(data: ColumnItem[]): boolean {
  return data.some((item) => !String(item.column_comment ?? '').trim());
}

interface ColumnProcessFlowProps {
  datasetId: number;
  datasetName?: string;
  datasetDescription?: string;
  owner?: string;
  lastUpdated?: string;
}

// 数据集信息卡片组件
interface DatasetHeaderCardProps {
  datasetName?: string;
  description?: string;
  owner?: string;
  lastUpdated?: string;
  datasetId: number;
  status?: 'normal' | 'error';
}

const DatasetHeaderCard: React.FC<DatasetHeaderCardProps> = ({
  datasetName = '数据集',
  description = '',
  owner = '-',
  lastUpdated = '-',
  datasetId,
  status = 'normal',
}) => {
  const { t } = useTranslation();
  return (
    <Card style={{ marginBottom: 24 }}>
      <div style={{ marginBottom: 12 }}>
        <Space align="center">
          <Title level={4} style={{ margin: 0 }}>{datasetName}</Title>
          <Tag color={status === 'normal' ? 'blue' : 'red'}>
            {status === 'normal' ? t('columnProcess.normal') : t('columnProcess.error')}
          </Tag>
        </Space>
      </div>
      <Text type="secondary" style={{ display: 'block', marginBottom: 16, lineHeight: 1.6 }}>
        {description || t('columnProcess.defaultDescription')}
      </Text>
      <div style={{ display: 'flex', alignItems: 'center', gap: 24, color: '#8c8c8c', fontSize: 13 }}>
        <Space size={4}>
          <Avatar size={20} icon={<UserOutlined />} style={{ backgroundColor: '#f0f0f0', color: '#8c8c8c' }} />
          <span>{t('columnProcess.owner', { owner })}</span>
        </Space>
        <span>{t('columnProcess.updatedAt', { time: lastUpdated })}</span>
        <span>ID: ds_{datasetId}</span>
      </div>
    </Card>
  );
};



const ColumnProcessFlow: React.FC<ColumnProcessFlowProps> = ({
  datasetId,
  datasetName,
  datasetDescription,
  owner = '-',
  lastUpdated = '-',
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const dimensionTypeOptions = useMemo(
    () => translateOptions(t, DIMENSION_TYPE, 'dimension.typeOptions'),
    [t],
  );
  const [currentStep, setCurrentStep] = useState(0);
  const [loading, setLoading] = useState(false);
  const [tableData, setTableData] = useState<ColumnItem[]>([]);
  const [editableKeys, setEditableRowKeys] = useState<React.Key[]>([]);
  const [completed, setCompleted] = useState(false);
  const actionRef = React.useRef<ActionType | undefined>(undefined);

  // Step 1: 预览Column元数据
  const fetchPreviewData = useCallback(async () => {
    setLoading(true);
    try {
      const res = await previewColumnMeta(datasetId);
      setTableData(addEditableKeys(resolveColumnItems(res)));
      // 默认不进入编辑状态，用户点击编辑按钮后才编辑当前行
      setEditableRowKeys([]);
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setLoading(false);
    }
  }, [datasetId]);

  // 组件挂载时获取预览数据
  useEffect(() => {
    fetchPreviewData();
  }, [fetchPreviewData]);

  // 确认入库 - Step 1
  const handleConfirmStorage = async () => {
    if (hasBlankColumnComment(tableData)) {
      message.error(t('column.inputComment'));
      return;
    }
    setLoading(true);
    try {
      const res = await confirmColumnMetaStorage(datasetId, tableData);
      const storedData = resolveColumnItems(res, tableData);
      message.success(t('columnProcess.storageSuccess'));
      setTableData(storedData);
      setCurrentStep(1);
      // 自动触发维度推理
      handleInferDimensions(storedData);
    } catch {
      // API 错误由响应拦截器统一提示
      setLoading(false);
    }
  };

  // 维度推理 - Step 2
  const handleInferDimensions = async (currentData?: ColumnItem[]) => {
    setLoading(true);
    try {
      const data = currentData || tableData;
      const res = await inferDimensions(datasetId, data);
      setTableData(addEditableKeys(resolveColumnItems(res, data)));
      setEditableRowKeys([]);
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setLoading(false);
    }
  };

  // 确认维度 - Step 2
  const handleConfirmDimensions = async () => {
    if (hasBlankColumnComment(tableData)) {
      message.error(t('column.inputComment'));
      return;
    }
    setLoading(true);
    try {
      const res = await confirmDimensions(datasetId, tableData);
      const confirmedData = resolveColumnItems(res, tableData);
      message.success(t('columnProcess.dimensionConfirmSuccess'));
      setTableData(confirmedData);
      setCurrentStep(2);
      // 自动触发样本补全
      handleCompleteSamples(confirmedData);
    } catch {
      // API 错误由响应拦截器统一提示
      setLoading(false);
    }
  };

  // 样本补全 - Step 3
  const handleCompleteSamples = async (currentData?: ColumnItem[]) => {
    setLoading(true);
    try {
      const data = currentData || tableData;
      const res = await completeSamples(datasetId);
      setTableData(addEditableKeys(resolveColumnItems(res, data)));
      setEditableRowKeys([]);
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setLoading(false);
    }
  };

  // 确认样本 - Step 3
  const handleConfirmSamples = async () => {
    if (hasBlankColumnComment(tableData)) {
      message.error(t('column.inputComment'));
      return;
    }
    setLoading(true);
    try {
      await confirmSamples(datasetId, tableData);
      message.success(t('columnProcess.sampleConfirmSuccess'));
      setCompleted(true);
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setLoading(false);
    }
  };

  // 上一步
  const handlePrevStep = () => {
    if (currentStep > 0) {
      setCurrentStep(currentStep - 1);
    }
  };

  // 取消/返回数据集管理
  const handleCancel = () => {
    navigate('/');
  };

  // 返回列管理页面
  const handleBackToManage = () => {
    navigate(`/column?datasetId=${datasetId}`);
  };

  // 下一步/确认操作
  const handleNextStep = () => {
    if (currentStep === 0) {
      handleConfirmStorage();
    } else if (currentStep === 1) {
      handleConfirmDimensions();
    } else if (currentStep === 2) {
      handleConfirmSamples();
    }
  };

  // 获取确认按钮文字
  const getConfirmButtonText = () => {
    if (currentStep === 2) {
      return t('columnProcess.confirmComplete');
    }
    return t('columnProcess.confirmNext');
  };

  // Step 1 列配置：基础列信息（匹配设计图样式）
  const step1Columns: ProColumns<ColumnItem>[] = [
    {
      title: t('columnProcess.columnName'),
      dataIndex: 'column_name',
      width: 150,
      render: (_, record) => (
        <Text code style={{ fontFamily: 'monospace', fontSize: 13 }}>
          {record.column_name}
        </Text>
      ),
    },
    {
      title: t('columnProcess.columnCnName'),
      dataIndex: 'column_name_cn',
      width: 140,
    },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      width: 130,
      valueType: 'select',
      fieldProps: {
        options: DATA_TYPE_OPTIONS,
      },
      render: (_, record) => (
        <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 13 }}>
          {record.data_type || '-'}
        </Text>
      ),
    },
    {
      title: t('column.columnType'),
      dataIndex: 'column_type',
      width: 100,
    },
    {
      title: t('columnProcess.primaryKey'),
      dataIndex: 'is_primary',
      width: 80,
      align: 'center',
      valueType: 'select',
      fieldProps: {
        options: [
          { label: t('common.yes'), value: 'Y' },
          { label: t('common.no'), value: 'N' },
        ],
      },
    },
    {
      title: t('columnProcess.nullable'),
      dataIndex: 'is_nullable',
      width: 80,
      align: 'center',
      valueType: 'select',
      fieldProps: {
        options: [
          { label: t('common.yes'), value: 'Y' },
          { label: t('common.no'), value: 'N' },
        ],
      },
    },
    {
      title: t('columnProcess.comment'),
      dataIndex: 'column_comment',
      width: 220,
      ellipsis: true,
      formItemProps: {
        rules: [{ required: true, whitespace: true, message: t('column.inputComment') }],
      },
    },
    {
      title: t('common.actions'),
      valueType: 'option',
      width: 200,
      render: (_text, record, _, action) => [
        <Button
          key="editable"
          type="link"
          onClick={() => {
            if (record.idx !== undefined) action?.startEditable?.(record.idx);
          }}
        >
          {t('common.edit')}
        </Button>,
      ],
    },
  ];

  // Step 2 列配置：突出维度类型
  const step2Columns: ProColumns<ColumnItem>[] = [
    {
      title: t('columnProcess.columnName'),
      dataIndex: 'column_name',
      width: 140,
      editable: false,
    },
    {
      title: t('columnProcess.columnCnName'),
      dataIndex: 'column_name_cn',
      width: 140,
    },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      width: 130,
      valueType: 'select',
      fieldProps: {
        options: DATA_TYPE_OPTIONS,
      },
    },
    {
      title: t('fields.dimensionType'),
      dataIndex: 'dimension_type',
      width: 100,
      valueType: 'select',
      fieldProps: {
        options: dimensionTypeOptions,
      },
      render: (_, record) => {
        return <>{translateOptionValue(t, record.dimension_type, 'dimension.typeOptions')}</>
      }
    },
    {
      title: t('column.columnType'),
      dataIndex: 'column_type',
      width: 100,
    },
    {
      title: t('columnProcess.comment'),
      dataIndex: 'column_comment',
      width: 200,
      ellipsis: true,
      formItemProps: {
        rules: [{ required: true, whitespace: true, message: t('column.inputComment') }],
      },
    },
    {
      title: t('common.actions'),
      valueType: 'option',
      width: 200,
      render: (_text, record, _, action) => [
        <Button
          key="editable"
          type="link"
          onClick={() => {
            if (record.idx !== undefined) action?.startEditable?.(record.idx);
          }}
        >
          {t('common.edit')}
        </Button>,
      ],
    },
  ];

  // Step 3 列配置：突出样本值
  const step3Columns: ProColumns<ColumnItem>[] = [
    {
      title: t('columnProcess.columnName'),
      dataIndex: 'column_name',
      width: 140,
      editable: false,
    },
    {
      title: t('columnProcess.columnCnName'),
      dataIndex: 'column_name_cn',
      width: 140,
    },
    {
      title: t('fields.dimensionType'),
      dataIndex: 'dimension_type',
      width: 100,
      valueType: 'select',
      fieldProps: {
        options: dimensionTypeOptions,
      },
      render: (_, record) => <>{translateOptionValue(t, record.dimension_type, 'dimension.typeOptions')}</>
    },
    {
      title: t('columnProcess.comment'),
      dataIndex: 'column_comment',
      width: 180,
      ellipsis: true,
      formItemProps: {
        rules: [{ required: true, whitespace: true, message: t('column.inputComment') }],
      },
    },
    {
      title: t('column.samples'),
      dataIndex: 'samples',
      width: 200,
      ellipsis: true,
    },
    {
      title: t('fields.dataType'),
      dataIndex: 'data_type',
      width: 120,
    },
    {
      title: t('columnProcess.nullable'),
      dataIndex: 'is_nullable',
      width: 120,
      align: 'center',
      valueType: 'select',
      fieldProps: {
        options: [
          { label: t('common.yes'), value: 'Y' },
          { label: t('common.no'), value: 'N' },
        ],
      },
    },
    {
      title: t('columnProcess.primaryKey'),
      dataIndex: 'is_primary',
      width: 120,
      align: 'center',
      valueType: 'select',
      fieldProps: {
        options: [
          { label: t('common.yes'), value: 'Y' },
          { label: t('common.no'), value: 'N' },
        ],
      },
    },
    {
      title: 'samples',
      dataIndex: 'samples',
      width: 180,
      ellipsis: true,
    },
    {
      title: t('fields.enums'),
      dataIndex: 'column_enums',
      width: 180,
      ellipsis: true,
    },
    {
      title: t('column.enumDescription'),
      dataIndex: 'column_enums_description',
      width: 180,
      ellipsis: true,
    },
    {
      title: t('common.actions'),
      valueType: 'option',
      width: 200,
      render: (_text, record, _, action) => [
        <Button
          key="editable"
          type="link"
          onClick={() => {
            if (record.idx !== undefined) action?.startEditable?.(record.idx);
          }}
        >
          {t('common.edit')}
        </Button>,
      ],
    },
  ];

  // 根据步骤获取列配置
  const getColumns = (): ProColumns<ColumnItem>[] => {
    switch (currentStep) {
      case 0:
        return step1Columns;
      case 1:
        return step2Columns;
      case 2:
        return step3Columns;
      default:
        return step1Columns;
    }
  };

  // 获取当前步骤的标题说明
  const getStepDescription = () => {
    switch (currentStep) {
      case 0:
        return t('columnProcess.stepDescription.fetch');
      case 1:
        return t('columnProcess.stepDescription.dimension');
      case 2:
        return t('columnProcess.stepDescription.sample');
      default:
        return '';
    }
  };

  return (
    <div>
      {/* 数据集信息卡片 */}
      <DatasetHeaderCard
        datasetName={datasetName || t('columnProcess.defaultDatasetName')}
        description={datasetDescription}
        owner={owner}
        lastUpdated={lastUpdated}
        datasetId={datasetId}
        status="normal"
      />

      {/* 步骤条 */}
      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 32 }}>
        <Steps
          current={currentStep}
          style={{ maxWidth: 500 }}
          items={STEPS.map((step, index) => ({
            title: t(`columnProcess.steps.${step.key}`),
            status: completed ? 'finish' : index < currentStep ? 'finish' : index === currentStep ? 'process' : 'wait',
          }))}
        />
      </div>

      {/* 数据列配置区域 */}
      <div style={{ marginBottom: 16 }}>
        <Title level={5} style={{ marginBottom: 4 }}>{t('columnProcess.columnConfig')}</Title>
        <Text type="secondary" style={{ fontSize: 13 }}>
          {getStepDescription()}
        </Text>
      </div>

      <div style={{ marginBottom: 24 }}>
        {completed ? (
          <Alert
            message={t('columnProcess.completedTitle')}
            description={t('columnProcess.completedDescription')}
            type="success"
            showIcon
            style={{ marginBottom: 24 }}
          />
        ) : (
          <Spin spinning={loading} tip={t('columnProcess.processing')}>
            <EditableProTable<ColumnItem>
              rowKey="idx"
              actionRef={actionRef}
              columns={getColumns()}
              value={tableData}
              onChange={(value) => setTableData([...value])}
              search={false}
              options={{ density: false }}
              pagination={false}
              scroll={{ x: 900 }}
              recordCreatorProps={false}
              bordered
              editable={{
                type: 'single',
                editableKeys,
                onChange: setEditableRowKeys,
                onSave: async (rowKey, data) => {
                  // 更新表格数据
                  setTableData((prev) =>
                    prev.map((item) => (item.idx === rowKey ? data : item))
                  );
                },
              }}
              tableStyle={{
                borderRadius: 0,
              }}
            />
          </Spin>
        )}
      </div>

      {/* 底部操作按钮 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, marginTop: 24 }}>
        {completed ? (
          <Space>
            <Button onClick={handleBackToManage}>{t('columnProcess.backToColumn')}</Button>
            <Button type="primary" onClick={handleCancel}>{t('dimensionValue.backToDataset')}</Button>
          </Space>
        ) : (
          <>
            <Button onClick={handleCancel}>{t('common.cancel')}</Button>
            {currentStep > 0 &&  <Button type="default" onClick={handlePrevStep}>{t('columnProcess.previous')}</Button>}
            <Button type="primary" onClick={handleNextStep} loading={loading}>
              {getConfirmButtonText()}
            </Button>
          </>
        )}
      </div>
    </div>
  );
};

export default ColumnProcessFlow;
