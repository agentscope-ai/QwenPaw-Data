import { PlusOutlined } from '@/design';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { Button, Popconfirm, Tag, message } from '@/design';
import { ProTable } from '@/design';
import type { ActionType, ProColumns, ProFormInstance } from '@/design';
import { useCascadeFilterOptions } from '@/hooks/useFilterOptions';
import { useModal } from '@/hooks/useModal';
import { ROUTES } from '@/router';
import { deleteMetric, queryMetricList } from '@/services/metricLib';
import { useHasCapability } from '@/store/authStore';
import { useBusinessDomainOptions } from '@/store';
import type { MetricLibItem, QueryMetricLibParams } from '@/types/metricLib';
import { deleteExtraDelete, toOptionalNumber, toOptionalString } from '@/utils';
import { readUrlSearchValues } from '@/utils/urlSearch';
import {
  loadSemanticDeepLinkInOrder,
  locateSemanticRecord,
  parseSemanticObjectId,
} from '@/utils/semanticDeepLink';
import React, { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ActionMetricModal from './components/ActionMetricModal';

type MetricDeepLink = QueryMetricLibParams & { metric_id?: number };

const MetricLib: React.FC<{ datasourceId?: string }> = ({ datasourceId }) => {
  const { t } = useTranslation();
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [deepLinkValues] = useState(() =>
    readUrlSearchValues<MetricDeepLink>(searchParams, {
      domain_id: { parse: parseSemanticObjectId },
      metric_id: { parse: parseSemanticObjectId },
      metric_name: {},
    }),
  );
  const initialSearchValues: QueryMetricLibParams = {
    domain_id: deepLinkValues.domain_id,
    metric_name: deepLinkValues.metric_name,
  };
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const { modal, showModal } = useModal(ActionMetricModal);
  const { loadDomains } = useCascadeFilterOptions();
  const domainOptions = useBusinessDomainOptions();

  const handleDelete = async (record: MetricLibItem) => {
    await deleteMetric(record.metric_id);
    message.success(t('common.deleteSuccess'));
    actionRef.current?.reload();
  };

  const columns: ProColumns<MetricLibItem>[] = [
    {
      title: t('fields.businessDomain'),
      dataIndex: 'domain_id',
      valueType: 'select',
      fieldProps: { options: domainOptions, showSearch: true, allowClear: true },
      width: 140,
      render: (_, record) => record.domain_name,
    },
    { title: t('fields.metricName'), dataIndex: 'metric_name', hideInTable: true },
    { title: t('fields.metricName'), dataIndex: 'metric_name', search: false, width: 160 },
    {
      title: t('fields.description'),
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
      width: 200,
      render: (_, record) => (
        <>
          {canWrite ? (
            <Button
              type="link"
              onClick={() =>
                showModal({
                  title: t('metric.editTitle'),
                  record,
                  callback: () => actionRef.current?.reload(),
                })
              }
            >
              {t('common.edit')}
            </Button>
          ) : null}
          {canWrite ? (
            <Popconfirm
              title={t('common.confirmDeleteRecord')}
              onConfirm={() => void handleDelete(record)}
            >
              <Button type="link" danger>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          ) : null}
          <Button
            type="link"
            onClick={() =>
              navigate(
                `${ROUTES.METRIC_FORMULA_LIB}?domain_id=${record.domain_id}&metric_id=${record.metric_id}`,
              )
            }
          >
            {t('routes.metricFormula')}
          </Button>
        </>
      ),
    },
  ];

  return (
    <>
      <ProTable<MetricLibItem>
        rowKey="metric_id"
        actionRef={actionRef}
        formRef={formRef}
        form={{ initialValues: initialSearchValues }}
        columns={columns}
        request={async (params) => {
          const requestData: QueryMetricLibParams = {
            page: params.current,
            size: params.pageSize,
            datasource_id: datasourceId,
            domain_id: toOptionalNumber(params.domain_id),
            metric_name: toOptionalString(params.metric_name),
          };
          if (deepLinkValues.metric_id && requestData.domain_id === deepLinkValues.domain_id) {
            const match = await loadSemanticDeepLinkInOrder([() => loadDomains()], () =>
              locateSemanticRecord({
                targetId: deepLinkValues.metric_id!,
                getRecordId: (record: MetricLibItem) => record.metric_id,
                size: params.pageSize,
                loadPage: (page, size) =>
                  queryMetricList({
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
          const result = await queryMetricList(deleteExtraDelete(requestData));
          return { data: result.records, success: true, total: result.total };
        }}
        scroll={{ x: 1250 }}
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
                  templateObject="metric"
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
                      title: t('metric.addTitle'),
                      initialFilter: {
                        domain_id: values.domain_id,
                      },
                      callback: () => actionRef.current?.reload(),
                    });
                  }}
                >
                  {t('metric.add')}
                </Button>,
              ]
            : []),
        ]}
      />
      {modal}
    </>
  );
};

export default MetricLib;
