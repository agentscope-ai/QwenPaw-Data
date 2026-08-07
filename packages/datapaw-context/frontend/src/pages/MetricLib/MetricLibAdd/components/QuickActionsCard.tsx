import React from 'react';
import { Card, Row, Col, Button, Upload, Popconfirm } from '@/design';
import {
  PlusOutlined,
  DeleteOutlined,
  UploadOutlined,
  DownloadOutlined,
  ThunderboltOutlined,
} from '@/design';
import type { UploadProps } from '@/design';
import { useTranslation } from 'react-i18next';

interface QuickActionsCardProps {
  onAddRow: () => void;
  onClearTable: () => void;
  onImport: UploadProps['beforeUpload'];
  onDownloadTemplate: () => void;
  currentStep: number;
}

const QuickActionsCard: React.FC<QuickActionsCardProps> = ({
  onAddRow,
  onClearTable,
  onImport,
  onDownloadTemplate,
  currentStep,
}) => {
  const { t } = useTranslation();
  // 最后一步时禁用导入和下载模板功能
  const isFinalStep = currentStep === 2;
  return (
    <Card
      title={
        <>
          <ThunderboltOutlined style={{ marginRight: 8 }} />
          {t('metricAdd.quickActions')}
        </>
      }
      style={{ marginBottom: 16 }}
    >
      <Row gutter={[12, 12]}>
        <Col xs={24} sm={24} md={12} lg={12}>
          <Button
            block
            icon={<PlusOutlined />}
            onClick={onAddRow}
            style={{ height: 48 }}
          >
            <div>{t('metricAdd.addRow')}</div>
          </Button>
        </Col>
        <Col xs={24} sm={24} md={12} lg={12}>
          <Popconfirm
            title={t('metricAdd.confirmClearTitle')}
            description={t('metricAdd.confirmClearDescription')}
            onConfirm={onClearTable}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
            okButtonProps={{ danger: true }}
          >
            <Button
              block
              icon={<DeleteOutlined />}
              style={{ height: 48 }}
            >
              <div>{t('metricAdd.clearTable')}</div>
            </Button>
          </Popconfirm>
        </Col>
        <Col xs={24} sm={24} md={12} lg={12}>
          <Upload
            accept=".csv,.xlsx,.xls"
            beforeUpload={onImport}
            showUploadList={false}
            style={{ width: '100%' }}
            disabled={isFinalStep}
          >
            <Button
              block
              icon={<UploadOutlined />}
              style={{ height: 48, width: '100%' }}
              disabled={isFinalStep}
            >
              <div>{t('metricAdd.importFromTemplate')}</div>
            </Button>
          </Upload>
        </Col>
        <Col xs={24} sm={24} md={12} lg={12}>
          <Button
            block
            icon={<DownloadOutlined />}
            onClick={onDownloadTemplate}
            style={{ height: 48 }}
            disabled={isFinalStep}
          >
            <div>{t('metricAdd.downloadTemplate')}</div>
          </Button>
        </Col>
      </Row>
    </Card>
  );
};

export default QuickActionsCard;
