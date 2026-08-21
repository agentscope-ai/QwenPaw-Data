import React from 'react';
import { Card, Space, Typography, Statistic, Row, Col } from '@/design';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

// 数据集信息卡片组件
export interface DatasetInfoCardProps {
  datasetName?: string;
  owner?: string;
  lastUpdated?: string;
  totalColumns?: number;
}

const DatasetInfoCard: React.FC<DatasetInfoCardProps> = ({
  datasetName = '-',
  owner = '-',
  lastUpdated = '-',
  totalColumns = 0,
}) => {
  const { t } = useTranslation();
  return (
    <Card style={{ marginBottom: 16 }}>
      <Row gutter={24} align="middle">
        <Col flex="auto">
          <Space size="large">
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t('fields.datasetName')}</Text>
              <div style={{ fontWeight: 500, fontSize: 16, color: '#0D76FD' }}>{datasetName}</div>
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t('columnProcess.ownerLabel')}</Text>
              <div>{owner}</div>
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t('columnProcess.lastUpdated')}</Text>
              <div>{lastUpdated}</div>
            </div>
          </Space>
        </Col>
        <Col>
          <Statistic title={t('columnProcess.totalColumns')} value={totalColumns} />
        </Col>
      </Row>
    </Card>
  );
};

export default DatasetInfoCard;
