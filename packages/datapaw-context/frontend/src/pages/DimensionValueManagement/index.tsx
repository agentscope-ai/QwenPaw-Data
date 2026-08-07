import { previewDimensionValue, confirmDimensionValueStorage, queryDimensionValueList, deleteDimensionValue } from '@/services/dimensionValueManagement';
import type { DimensionValueItem, QueryDimensionValueParams } from '@/types/dimensionValueManagement';
import { deleteExtraDelete, omitKeys } from '@/utils';
import { normalizeListResponse, extractTotal } from '@/utils/listResponse';
import { ArrowLeftOutlined, SaveOutlined } from '@/design';
import { ProColumns, ProTable, EditableProTable } from '@/design';
import type { ActionType } from '@/design';
import { Button, Popconfirm, message, Tag, Space, Spin, Alert } from '@/design';
import React, { useRef, useState, useEffect, useMemo } from 'react';
import { useSearchParams, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

const DimensionValueManagement: React.FC = () => {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const actionRef = useRef<ActionType | undefined>(undefined);

  // URL 参数
  const mode = searchParams.get('mode');
  const datasetIdsParam = searchParams.get('datasetIds');
  const isPreviewMode = mode === 'preview' && !!datasetIdsParam;

  // 预览模式状态
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewData, setPreviewData] = useState<DimensionValueItem[]>([]);
  const [, setEditableRowKeys] = useState<React.Key[]>([]);
  const [confirmLoading, setConfirmLoading] = useState(false);

  // 按 dimensionName 分组
  const groupedPreviewData = useMemo(() => {
    const groups: Record<string, DimensionValueItem[]> = {};
    previewData.forEach((item) => {
      const key = item.dimension_name || t('dimensionValue.unknownDimension');
      if (!groups[key]) {
        groups[key] = [];
      }
      groups[key].push(item);
    });
    return groups;
  }, [previewData, t]);

  // 预览模式：加载数据
  useEffect(() => {
    if (isPreviewMode && datasetIdsParam) {
      const datasetIds = datasetIdsParam.split(',').map(Number).filter(Boolean);
      if (datasetIds.length > 0) {
        loadPreviewData(datasetIds);
      }
    }
  }, [isPreviewMode, datasetIdsParam]);

  const loadPreviewData = async (datasetIds: number[]) => {
    setPreviewLoading(true);
    try {
      const result = await previewDimensionValue(datasetIds);
      const data = normalizeListResponse<DimensionValueItem>(result);
      // 为每条数据添加临时 key
      const dataWithKeys = data.map((item: DimensionValueItem, index: number) => ({
        ...item,
        _key: String(item.id || `temp_${index}`),
      }));
      setPreviewData(dataWithKeys);
      setEditableRowKeys(dataWithKeys.map((item: DimensionValueItem) => item._key!));
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setPreviewLoading(false);
    }
  };

  // 确认入库
  const handleConfirmStorage = async () => {
    if (previewData.length === 0) {
      message.warning(t('dimensionValue.noDataToStore'));
      return;
    }
    setConfirmLoading(true);
    try {
      // 移除临时 key
      const dataToSubmit = previewData.map((item) => omitKeys(item, ['_key']));
      await confirmDimensionValueStorage(dataToSubmit);
      message.success(t('dimensionValue.storageSuccess'));
      // 返回管理表格
      navigate('/dimension-value');
    } catch {
      // API 错误由响应拦截器统一提示
    } finally {
      setConfirmLoading(false);
    }
  };

  // 返回数据集管理
  const handleBackToDataset = () => {
    navigate('/');
  };

  // 删除单条记录
  const handleDelete = async (record: DimensionValueItem) => {
    try {
      await deleteDimensionValue(record.id);
      message.success(t('common.deleteSuccess'));
      actionRef.current?.reload();
    } catch {
      // API 错误由响应拦截器统一提示
    }
  };

  // 预览模式的可编辑表格列
  const editableColumns: ProColumns<DimensionValueItem>[] = [
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', editable: false, width: 120 },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', editable: false, width: 120 },
    { title: t('fields.calculateExpression'), dataIndex: 'calculate_expr', width: 150 },
    { title: t('fields.dimensionType'), dataIndex: 'dimension_type', width: 100 },
    { title: t('fields.dataType'), dataIndex: 'data_type', width: 100 },
    { title: t('fields.dimensionValue'), dataIndex: 'dimension_value', width: 150 },
    { title: t('fields.occurrenceCount'), dataIndex: 'dimension_occur_cnt', editable: false, width: 100 },
  ];

  // 管理表格列
  const columns: ProColumns<DimensionValueItem>[] = [
    { title: t('fields.datasetName'), dataIndex: 'datasetName', search: false, width: 120, render: (_, record) => <>{record.dataset_name || '-'}</> },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', width: 120 },
    { title: t('fields.calculateExpression'), dataIndex: 'calculate_expr', search: false, ellipsis: true, width: 150 },
    { title: t('fields.dimensionType'), dataIndex: 'dimension_type', search: false, width: 100 },
    { title: t('fields.dataType'), dataIndex: 'data_type', search: false, width: 100 },
    { title: t('fields.dimensionValue'), dataIndex: 'dimensionValue', width: 150, render: (_, record) => <>{record.dimension_value || '-'}</> },
    { title: t('fields.occurrenceCount'), dataIndex: 'dimension_occur_cnt', search: false, width: 100 },
    {
      title: t('common.actions'), search: false, dataIndex: 'action', fixed: 'right', width: 80,
      render: (_, record) => (
        <Popconfirm
          title={t('common.confirmDeleteRecord')}
          onConfirm={() => handleDelete(record)}
        >
          <Button size="small" type="link" danger>{t('common.delete')}</Button>
        </Popconfirm>
      ),
    },
  ];

  // 预览模式渲染
  if (isPreviewMode) {
    return (
      <>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={handleBackToDataset}>
              {t('dimensionValue.backToDataset')}
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={handleConfirmStorage}
              loading={confirmLoading}
              disabled={previewData.length === 0}
            >
              {t('dimensionValue.confirmStorage')}
            </Button>
          </Space>
        </div>
        <Spin spinning={previewLoading}>
          <Alert
            message={t('dimensionValue.previewMode')}
            description={t('dimensionValue.previewDescription')}
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
          {Object.entries(groupedPreviewData).map(([dimensionName, items]) => (
            <div key={dimensionName} style={{ marginBottom: 24 }}>
              <Tag color="blue" style={{ marginBottom: 8, fontSize: 14, padding: '4px 12px' }}>
                {t('dimensionValue.groupTitle', { name: dimensionName, count: items.length })}
              </Tag>
              <EditableProTable<DimensionValueItem>
                rowKey="_key"
                value={items}
                columns={editableColumns}
                recordCreatorProps={false}
                editable={{
                  type: 'multiple',
                  editableKeys: items.map((item: DimensionValueItem) => item._key).filter((key): key is string => key !== undefined),
                  onChange: setEditableRowKeys,
                  onValuesChange: (_record, recordList) => {
                    // 更新预览数据
                    setPreviewData((prev) => {
                      const newData = [...prev];
                      recordList.forEach((updatedItem) => {
                        const index = newData.findIndex((item) => item._key === (updatedItem as DimensionValueItem)._key);
                        if (index > -1) {
                          newData[index] = updatedItem;
                        }
                      });
                      return newData;
                    });
                  },
                }}
                scroll={{ x: 1200 }}
                pagination={false}
              />
            </div>
          ))}
          {previewData.length === 0 && !previewLoading && (
            <Alert message={t('dimensionValue.noPreviewData')} type="warning" showIcon />
          )}
        </Spin>
      </>
    );
  }

  // 管理表格模式渲染
  return (
    <ProTable<DimensionValueItem>
        rowKey={(record: DimensionValueItem) => record.id}
        actionRef={actionRef}
        columns={columns}
        request={async (params) => {
          const requestData: QueryDimensionValueParams = {
            page: params.current,
            size: params.pageSize,
            ...params,
          };
          try {
            const result = await queryDimensionValueList(deleteExtraDelete(requestData));
            const list = normalizeListResponse<DimensionValueItem>(result);
            return {
              data: list,
              success: true,
              total: extractTotal(result) || list.length,
            };
          } catch {
            return { data: [], success: false, total: 0 };
          }
        }}
        scroll={{ x: 1400 }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50', '100'],
        }}
        search={{
          labelWidth: 'auto',
        }}
      />
  );
};

export default DimensionValueManagement;
