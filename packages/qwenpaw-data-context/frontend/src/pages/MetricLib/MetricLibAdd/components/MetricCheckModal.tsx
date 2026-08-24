import React from 'react';
import { Modal, Typography, Alert, Space, Divider } from '@/design';
import { WarningOutlined } from '@/design';
import type { MetricCheckResult } from '@/types/metricLib';
import { useTranslation } from 'react-i18next';

const { Text, Title } = Typography;

interface MetricCheckModalProps {
  title: string;
  visible: boolean;
  data: MetricCheckResult[];
  onCancel: () => void;
  callback: (type: 'button' | 'modal') => void;
}

const MetricCheckModal: React.FC<MetricCheckModalProps> = ({
  title,
  visible,
  data = [],
  onCancel,
  callback,
}) => {
  const { t } = useTranslation();
  // 处理"仍然创建"按钮点击
  const handleConfirm = () => {
    Modal.confirm({
      title: t('metricAdd.confirmCreate'),
      content: t('metricAdd.confirmDuplicateCreateContent'),
      onOk: () => {
        callback('modal');
        onCancel();
      },
    });
  };

  // 渲染重复检查结果
  const renderCheckResults = () => {
    if (!data || data.length === 0) {
      return (
        <Alert
          type="success"
          message={t('metricAdd.noDuplicateMetric')}
          showIcon
        />
      );
    }

    return (
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {data.map((item, index) => (
          <div key={index}>
            {/* 输入指标名作为分组标题 */}
            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
              <WarningOutlined style={{ color: '#faad14', marginRight: 8 }} />
              <Title level={5} style={{ margin: 0 }}>
                {t('metricAdd.metricLabel', { name: item.input_metric })}
              </Title>
              {item.domain && (
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  {t('metricAdd.domainLabel', { domain: item.domain })}
                </Text>
              )}
            </div>
            <Alert
              type="warning"
              message={item.suggestion}
              showIcon={false}
              style={{ backgroundColor: '#fffbe6', border: '1px solid #ffe58f' }}
            />
            {/* 分隔线（最后一个不显示） */}
            {index < data.length - 1 && <Divider style={{ margin: '16px 0' }} />}
          </div>
        ))}
      </Space>
    );
  };

  return (
    <Modal
      title={title}
      open={visible}
      onCancel={onCancel}
      okText={t('metricAdd.stillCreate')}
      cancelText={t('common.cancel')}
      onOk={handleConfirm}
      width={680}
    >
      {renderCheckResults()}
    </Modal>
  );
};

export default MetricCheckModal;
