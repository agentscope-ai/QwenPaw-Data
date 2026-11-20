import { PlusOutlined } from '@/design';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { Button, Modal, Tag, message } from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns, ProFormInstance } from '@/design';
import { useModal } from '@/hooks/useModal';
import { ROUTES } from '@/router';
import { deleteDimension, queryDimensionList } from '@/services/dimensionManagement';
import { useBusinessDomainOptions } from '@/store';
import { useHasCapability } from '@/store/authStore';
import type { DimensionItem, QueryDimensionParams } from '@/types/dimensionManagement';
import { deleteExtraDelete, toOptionalNumber, toOptionalString } from '@/utils';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ActionDimensionDrawer from './components/ActionDimensionDrawer';

type DimensionDeepLink = QueryDimensionParams & { dimension_id?: number };

const DimensionManagement: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<DimensionDeepLink>(searchParams, {
      domain_id: { parse: parseSemanticObjectId },
      dimension_id: { parse: parseSemanticObjectId },
      dimension_name: {},
    }),
  );
  const initialSearchValues: QueryDimensionParams = {
    domain_id: deepLinkValues.domain_id,
    dimension_name: deepLinkValues.dimension_name,
  };
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const navigate = useNavigate();
  const { modal, showModal } = useModal(ActionDimensionDrawer);
  const { loadDomains } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();

  useEffect(() => {
    void loadDomains();
  }, [loadDomains]);

  const handleDelete = (record: DimensionItem) => {
    Modal.confirm({
      title: t('common.confirmDeleteTitle'),
      content: t('dimension.confirmDelete', { name: record.dimension_name }),
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteDimension(record.dimension_id);
        message.success(t('common.deleteSuccess'));
        actionRef.current?.reload();
      },
    });
  };

  const columns: ProColumns<DimensionItem>[] = [
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      hideInTable: true,
      valueType: 'select',
      fieldProps: { options: domainOptions, showSearch: true, allowClear: true },
    },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', hideInTable: true },
    { title: t('fields.businessDomain'), dataIndex: 'domain_name', search: false, width: 140 },
    { title: t('fields.dimensionName'), dataIndex: 'dimension_name', search: false, width: 150 },
    {
      title: t('fields.dimensionDescription'),
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      width: 220,
    },
    {
      title: t('fields.synonyms'),
      dataIndex: 'synonyms',
      search: false,
      ellipsis: true,
      width: 180,
    },
    { title: t('fields.dimensionType'), dataIndex: 'dimension_type', search: false, width: 140 },
    {
      title: 'child_dimensions',
      dataIndex: 'child_dimensions',
      search: false,
      ellipsis: true,
      width: 220,
      render: (_, record) => record.child_dimensions?.split('$$$').join(' → ') || '-',
    },
    {
      title: 'priority',
      dataIndex: 'priority',
      search: false,
      width: 110,
      render: (_, record) => <Tag>{record.priority}</Tag>,
    },
    {
      title: t('common.actions'),
      search: false,
      fixed: 'right',
      width: 190,
      render: (_, record) => (
        <>
          {canWrite ? (
            <Button
              type="link"
              onClick={() =>
                showModal({
                  title: t('dimension.editTitle'),
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
          <Button
            type="link"
            onClick={() =>
              navigate(
                `${ROUTES.DIMENSION_CALIBER}?domain_id=${record.domain_id}&dimension_id=${record.dimension_id}`,
              )
            }
          >
            {t('routes.dimensionCaliber')}
          </Button>
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<DimensionItem>
        rowKey="dimension_id"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        form={{ initialValues: initialSearchValues }}
        request={async (params) => {
          const requestData: QueryDimensionParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId,
            domain_id: toOptionalNumber(params.domain_id),
            dimension_name: toOptionalString(params.dimension_name),
          };
          if (deepLinkValues.dimension_id && requestData.domain_id === deepLinkValues.domain_id) {
            const match = await loadSemanticDeepLinkInOrder([() => loadDomains()], () =>
              locateSemanticRecord({
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
              }),
            );
            return {
              data: match ? [match.record] : [],
              success: true,
              total: match ? 1 : 0,
            };
          }
          const result = await queryDimensionList(deleteExtraDelete(requestData));
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1650 }}
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
                  templateObject="dimension"
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
                      title: t('dimension.addTitle'),
                      initialFilter: {
                        domain_id: values.domain_id,
                      },
                      callback: () => actionRef.current?.reload(),
                    });
                  }}
                >
                  {t('dimension.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {modal}
    </>
  );
};

export default DimensionManagement;
