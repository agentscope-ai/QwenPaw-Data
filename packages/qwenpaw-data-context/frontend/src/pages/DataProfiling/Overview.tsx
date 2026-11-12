import { Button } from '@/design';
import SemanticExcelActions from '@/components/SemanticExcelActions';
import { ProTable } from '@/design';
import type { ProColumns } from '@/design';
import ProfilingStatusTag from './ProfilingStatusTag';
import { queryProfilingOverview } from '@/services/dataProfiling';
import { useHasCapability } from '@/store/authStore';
import type { ProfilingOverviewParams, ProfilingOverviewRecord } from '@/types/dataProfiling';
import { deleteExtraDelete, toOptionalString } from '@/utils';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useState } from 'react';
import { Space } from '@/design';
import { readUrlSearchValues } from '@/utils/urlSearch';

const STAGE_STATUS_COLUMNS: Array<{
  dataIndex: keyof ProfilingOverviewRecord;
  titleKey: string;
}> = [
  { dataIndex: 'biz_domain_status', titleKey: 'dataProfiling.stages.bizDomain' },
  { dataIndex: 'dataset_status', titleKey: 'dataProfiling.stages.dataset' },
  { dataIndex: 'dataset_fields_status', titleKey: 'dataProfiling.stages.datasetFields' },
  { dataIndex: 'dimension_caliber_status', titleKey: 'dataProfiling.stages.dimensionCaliber' },
  { dataIndex: 'dimension_manage_status', titleKey: 'dataProfiling.stages.dimensionManage' },
  { dataIndex: 'metric_caliber_status', titleKey: 'dataProfiling.stages.metricCaliber' },
  { dataIndex: 'metric_manage_status', titleKey: 'dataProfiling.stages.metricManage' },
];

export default function DataProfilingOverview() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [initialSearchValues] = useState(() =>
    readUrlSearchValues<ProfilingOverviewParams>(searchParams, {
      datasource_id: { aliases: ['datasourceId'] },
      datasource_name: { aliases: ['datasourceName'] },
      datasource_type: { aliases: ['datasourceType'] },
    }),
  );
  const canWrite = useHasCapability('datasource:write');
  const canExcel = useHasCapability('excel_import');

  const openWorkbench = (record: ProfilingOverviewRecord) => {
    const params = new URLSearchParams();
    if (record.datasource_name) params.set('name', record.datasource_name);
    if (record.datasource_type) params.set('type', record.datasource_type);
    navigate(`/data-profiling/${encodeURIComponent(record.datasource_id)}?${params.toString()}`);
  };

  const actionLabel = (record: ProfilingOverviewRecord) => {
    if (!canWrite) return t('dataProfiling.modelingActions.view');
    if (record.overall_status === 'NOT_STARTED') return t('dataProfiling.modelingActions.start');
    if (record.overall_status === 'COMPLETED') return t('dataProfiling.modelingActions.update');
    return t('dataProfiling.modelingActions.continue');
  };

  const columns: ProColumns<ProfilingOverviewRecord>[] = [
    {
      title: t('fields.dataSourceName'),
      dataIndex: 'datasource_name',
      width: 180,
    },
    {
      title: t('dataProfiling.dataSourceIdentifier'),
      dataIndex: 'datasource_id',
      width: 210,
    },
    {
      title: t('fields.dataSourceType'),
      dataIndex: 'datasource_type',
      valueType: 'select',
      width: 140,
      fieldProps: {
        options: [
          { label: 'MySQL', value: 'mysql' },
          { label: 'PostgreSQL', value: 'postgresql' },
          { label: 'ODPS', value: 'odps' },
        ],
      },
    },
    {
      title: t('dataProfiling.overallStatus'),
      dataIndex: 'overall_status',
      search: false,
      width: 120,
      render: (_, record) => <ProfilingStatusTag t={t} status={record.overall_status} />,
    },
    ...STAGE_STATUS_COLUMNS.map(({ dataIndex, titleKey }) => ({
      title: t(titleKey),
      dataIndex,
      search: false,
      width: 130,
      render: (_: unknown, record: ProfilingOverviewRecord) => (
        <ProfilingStatusTag
          t={t}
          status={record[dataIndex] as ProfilingOverviewRecord['biz_domain_status']}
        />
      ),
    })),
    {
      title: t('common.actions'),
      dataIndex: 'action',
      search: false,
      fixed: 'right',
      width: 220,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" style={{ paddingInline: 4 }} onClick={() => openWorkbench(record)}>
            {actionLabel(record)}
          </Button>
          {canExcel ? (
            <SemanticExcelActions
              datasourceId={record.datasource_id}
              templateObject="all"
              showExport
              displayMode="dropdown"
              buttonType="link"
              dropdownLabel={t('dataProfiling.modelingActions.semanticModel')}
              dropdownTrigger="hover"
            />
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <ProTable<ProfilingOverviewRecord>
      rowKey="datasource_id"
      columns={columns}
      form={{ initialValues: initialSearchValues }}
      scroll={{ x: 1570 }}
      request={async (params) => {
        const requestParams: ProfilingOverviewParams = deleteExtraDelete({
          datasource_id: toOptionalString(params.datasource_id),
          datasource_name: toOptionalString(params.datasource_name),
          datasource_type: toOptionalString(params.datasource_type),
          page: params.current,
          size: params.pageSize,
        });
        const result = await queryProfilingOverview(requestParams);
        return {
          data: result.records,
          total: result.total,
          success: true,
        };
      }}
      pagination={{ defaultPageSize: 20, showSizeChanger: true }}
    />
  );
}
