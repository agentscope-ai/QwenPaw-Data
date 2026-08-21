import React from 'react';
import { Card, Steps } from '@/design';
import { METRIC_ADD_STEPS } from '../useMetricAdd';
import { useTranslation } from 'react-i18next';

interface StepsCardProps {
  currentStep: number;
}

const StepsCard: React.FC<StepsCardProps> = ({ currentStep }) => {
  const { t } = useTranslation();
  return (
    <Card style={{ marginBottom: 16 }}>
      <Steps
        current={currentStep}
        items={METRIC_ADD_STEPS.map((step) => ({
          title: t(step.titleKey),
          description: t(step.descriptionKey),
        }))}
      />
    </Card>
  );
};

export default StepsCard;
