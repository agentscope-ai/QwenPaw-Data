import SemanticExcelActions from '@/components/SemanticExcelActions';
import { Card, Select, Space, Typography } from '@/design';
import { useDataSourceFilterOptions } from '@/hooks/useFilterOptions';
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

const ExcelImportPage: React.FC = () => {
  const { t } = useTranslation();
  const { options: datasourceOptions, loading } = useDataSourceFilterOptions('full');
  const [datasourceId, setDatasourceId] = useState<string>();

  return (
    <div className="context-page-stack">
      <Card className="context-page-surface">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Select
            value={datasourceId}
            options={datasourceOptions}
            loading={loading}
            showSearch
            allowClear
            optionFilterProp="label"
            placeholder={t('excelImport.selectDatasource')}
            onChange={setDatasourceId}
            style={{ width: 360, maxWidth: '100%' }}
          />
          <SemanticExcelActions datasourceId={datasourceId} templateObject="all" showExport />
          <Typography.Text type="secondary">{t('excelImport.uploadHint')}</Typography.Text>
        </Space>
      </Card>
    </div>
  );
};

export default ExcelImportPage;
