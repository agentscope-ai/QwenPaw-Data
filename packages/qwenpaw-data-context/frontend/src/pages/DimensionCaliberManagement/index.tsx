import { PlusOutlined } from '@/design';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { Button, Modal, message } from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns, ProFormInstance } from '@/design';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { useModal } from '@/hooks/useModal';
import {
  deleteDimensionCaliber,
  queryDimensionCaliberList,
  queryDimensionList,
} from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import { useHasCapability } from '@/store/authStore';
import type {
  DimensionCaliberItem,
  DimensionItem,
  QueryDimensionCaliberParams,
} from '@/types/dimensionManagement';
import {
  deleteExtraDelete,
  formatDatasourceLabel,
  toOptionalNumber,
  toOptionalString,
} from '@/utils';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  loadSemanticRowsByParent,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import ActionDimensionCaliberDrawer from './components/ActionDimensionCaliberDrawer';

type DimensionCaliberDeepLink = QueryDimensionCaliberParams & { dimension_id?: number };

const DimensionCaliberManagement: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<DimensionCaliberDeepLink>(searchParams, {
      domain_id: { parse: parseSemanticObjectId },
      dimension_id: { parse: parseSemanticObjectId },
      dataset_id: { parse: parseSemanticObjectId },
      dimension_name: {},
    }),
  );
  const initialSearchValues = useMemo<QueryDimensionCaliberParams>(
    () => ({
      domain_id: deepLinkValues.domain_id,
      dataset_id: deepLinkValues.dataset_id,
      dimension_name: deepLinkValues.dimension_name,
    }),
    [deepLinkValues.dataset_id, deepLinkValues.dimension_name, deepLinkValues.domain_id],
  );
  const { modal, showModal } = useModal(ActionDimensionCaliberDrawer);
  const { loadDomains, loadDimensionNames, loadDatasets } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();
  const [dimensionOptions, setDimensionOptions] = useState<Array<{ label: string; value: string }>>(
    [],
  );
  const [datasetOptions, setDatasetOptions] = useState<Array<{ label: string; value: number }>>([]);

  useEffect(() => {
    const initialize = async () => {
      await loadDomains();
      if (initialSearchValues.domain_id) {
        const [dimensions, datasets] = await Promise.all([
          loadDimensionNames(initialSearchValues.domain_id),
          loadDatasets(initialSearchValues.domain_id, datasourceId),
        ]);
        setDimensionOptions(dimensions);
        setDatasetOptions(datasets);
      }
    };
    void initialize();
  }, [datasourceId, initialSearchValues, loadDatasets, loadDimensionNames, loadDomains]);

  const handleDomainChange = async (domainId?: number) => {
    formRef.current?.setFieldsValue({ dimension_name: undefined, dataset_id: undefined });
    if (!domainId) {
      setDimensionOptions([]);
      setDatasetOptions([]);
      return;
    }
    const [dimensions, datasets] = await Promise.all([
      loadDimensionNames(domainId),
      loadDatasets(domainId, datasourceId),
    ]);
    setDimensionOptions(dimensions);
    setDatasetOptions(datasets);
  };

  const handleDelete = (record: DimensionCaliberItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: (
        <div>
          <p>
            {t('dimensionCaliber.confirmDelete', {
              dimension: record.dimension_name,
              dataset: record.dataset_name,
            })}
          </p>
          <p style={{ color: '#faad14', marginBottom: 0 }}>{t('dimensionCaliber.deleteWarning')}</p>
        </div>
      ),
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteDimensionCaliber(record.id);
        message.success(t('common.deleteSuccess'));
        actionRef.current?.reload();
      },
    });
  };

  const columns: ProColumns<DimensionCaliberItem>[] = [
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: domainOptions,
        showSearch: true,
        allowClear: true,
        onChange: (value: number) => void handleDomainChange(value),
      },
    },
    {
      title: t('fields.dimensionName'),
      dataIndex: 'dimension_name',
      hideInTable: true,
      valueType: 'select',
      fieldProps: { options: dimensionOptions, showSearch: true, allowClear: true },
    },
    {
      title: t('fields.datasetName'),
      dataIndex: 'dataset_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: { options: datasetOptions, showSearch: true, allowClear: true },
    },
    {
      title: t('fields.dataSource'),
      dataIndex: 'datasource_name',
      search: false,
      width: 180,
      render: (_, record) => formatDatasourceLabel(record.datasource_name, record.datasource_id),
    },
    { title: t('fields.businessDomain'), dataIndex: 'domain_name', search: false, width: 140 },
    { title: t('fields.datasetName'), dataIndex: 'dataset_name', search: false, width: 180 },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', search: false, width: 150 },
    {
      title: t('fields.calculateExpression'),
      dataIndex: 'expr',
      search: false,
      ellipsis: true,
      width: 260,
    },
    {
      title: 'value_example',
      dataIndex: 'value_example',
      search: false,
      ellipsis: true,
      width: 220,
    },
    {
      title: t('common.actions'),
      search: false,
      fixed: 'right',
      width: 130,
      render: (_, record) => (
        <>
          {canWrite ? (
            <Button
              type="link"
              onClick={() =>
                showModal({
                  datasourceId,
                  title: t('dimensionCaliber.editTitle'),
                  record,
                  callback: () => actionRef.current?.reload(),
                })
              }
            >
              {t('common.edit')}
            </Button>
          ) : null}
          {canWrite ? (
            <Button type="link" danger onClick={() => handleDelete(record)}>
              {t('common.delete')}
            </Button>
          ) : null}
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<DimensionCaliberItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          const requestData: QueryDimensionCaliberParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId,
            domain_id: toOptionalNumber(params.domain_id),
            dataset_id: toOptionalNumber(params.dataset_id),
            dimension_name: toOptionalString(params.dimension_name),
          };
          if (deepLinkValues.dimension_id && requestData.domain_id === deepLinkValues.domain_id) {
            const resolved = await loadSemanticDeepLinkInOrder(
              [
                () => loadDomains(),
                async () => {
                  const [dimensions, datasets] = await Promise.all([
                    loadDimensionNames(deepLinkValues.domain_id!),
                    loadDatasets(deepLinkValues.domain_id!, datasourceId),
                  ]);
                  setDimensionOptions(dimensions);
                  setDatasetOptions(datasets);
                },
              ],
              () =>
                loadSemanticRowsByParent({
                  targetId: deepLinkValues.dimension_id!,
                  getRecordId: (record: DimensionItem) => record.dimension_id,
                  size: params.pageSize,
                  loadPage: (page, size) =>
                    queryDimensionList({
                      datasource_id: datasourceId,
                      domain_id: deepLinkValues.domain_id,
                      page,
                      size,
                    }),
                  loadRows: (dimension) => {
                    setDimensionOptions((current) =>
                      current.some((option) => option.value === dimension.dimension_name)
                        ? current
                        : [
                            ...current,
                            { label: dimension.dimension_name, value: dimension.dimension_name },
                          ],
                    );
                    formRef.current?.setFieldsValue({
                      domain_id: dimension.domain_id,
                      dimension_name: dimension.dimension_name,
                    });
                    return queryDimensionCaliberList({
                      datasource_id: datasourceId,
                      domain_id: dimension.domain_id,
                      dimension_name: dimension.dimension_name,
                      page: params.current,
                      size: params.pageSize,
                    });
                  },
                }),
            );
            return {
              data: resolved?.rows.records ?? [],
              success: true,
              total: resolved?.rows.total ?? 0,
            };
          }
          const result = await queryDimensionCaliberList(deleteExtraDelete(requestData));
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1450 }}
        pagination={{
          defaultPageSize: 20,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
        }}
        search={{ labelWidth: 'auto', defaultCollapsed: false }}
        options={{ density: false }}
        toolBarRender={() => [
          ...(canExcel
            ? [
                <SemanticExcelActions
                  key="excel"
                  templateObject="dataset_dimension"
                  datasourceId={datasourceId}
                  onImportSuccess={() => actionRef.current?.reload()}
                />,
              ]
            : []),
          ...(canWrite
            ? [
                <Button
                  key="add"
                  icon={<PlusOutlined />}
                  type="primary"
                  onClick={() => {
                    const values = formRef.current?.getFieldsValue?.() ?? {};
                    showModal({
                      datasourceId,
                      title: t('dimensionCaliber.addTitle'),
                      initialFilter: {
                        domain_id: values.domain_id,
                        dimension_name: values.dimension_name,
                        dataset_id: values.dataset_id,
                      },
                      callback: () => actionRef.current?.reload(),
                    });
                  }}
                >
                  {t('dimensionCaliber.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {modal}
    </>
  );
};

export default DimensionCaliberManagement;
