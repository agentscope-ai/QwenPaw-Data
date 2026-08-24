import React from 'react';
import { Card, Alert, Typography } from '@/design';
import { InfoCircleOutlined } from '@/design';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

const InputInstructionsCard: React.FC = () => {
  const { t } = useTranslation();
  return (
    <Card
      title={
        <>
          <InfoCircleOutlined style={{ marginRight: 8 }} />
          {t('metricAdd.instructionsTitle')}
        </>
      }
      style={{ marginBottom: 16 }}
    >
      <Alert
        message={t('metricAdd.instructionsTip')}
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
      />
      <ul style={{ paddingLeft: 16, margin: 0, color: '#666' }}>
        <li style={{ marginBottom: 8 }}>
          <Text type="secondary">
            <Text strong>{t('fields.metricName')}:</Text>{t('metricAdd.metricNameInstruction')}
          </Text>
        </li>
        <li style={{ marginBottom: 8 }}>
          <Text type="secondary">
            <Text strong>{t('metricAdd.domain')}:</Text>{t('metricAdd.domainInstruction')}
          </Text>
        </li>
        <li>
          <Text type="secondary">
            <Text strong>{t('fields.synonyms')}:</Text>{t('metricAdd.synonymsInstruction')}
          </Text>
        </li>
      </ul>
    </Card>
  );
};

export default InputInstructionsCard;
