import React from 'react';
import { Space, Button } from '@/design';
import {
  SearchOutlined,
  CheckOutlined,
  LeftOutlined,
} from '@/design';
import { useTranslation } from 'react-i18next';

interface BottomButtonsProps {
  currentStep: number;
  onPrevStep: () => void;
  onConsistencyCheck: () => void;
  onConfirmCreateLib: () => void;
  onConfirmCreate: () => void;
  onCancel: () => void;
  isCheckDisabled: boolean;
  isConfirmDisabled: boolean;
}

const BottomButtons: React.FC<BottomButtonsProps> = ({
  currentStep,
  onPrevStep,
  onConsistencyCheck,
  onConfirmCreate,
  onConfirmCreateLib,
  onCancel,
  isCheckDisabled,
}) => {
  const { t } = useTranslation();
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {/* 上一步按钮 - 只在第二步及以后显示 */}
      {currentStep > 0 && (
        <Button
          block
          size="large"
          icon={<LeftOutlined />}
          onClick={onPrevStep}
        >
          {t('columnProcess.previous')}
        </Button>
      )}

     {
      currentStep < 1 && (
         <Button
        type="primary"
        block
        size="large"
        icon={<SearchOutlined />}
        onClick={onConsistencyCheck}
      >
        {t('metricAdd.consistencyCheck')}
      </Button>
      )
     }
      {
        currentStep === 1 && (
          <Button
            block
            size="large"
            icon={<CheckOutlined />}
            onClick={onConfirmCreate}
            disabled={isCheckDisabled}
          >
            {t('metricAdd.confirmCreate')}
          </Button>
        )
      }
      {
        currentStep === 2 && (
          <Button
            block
            size="large"
            icon={<CheckOutlined />}
            onClick={onConfirmCreateLib}
            disabled={isCheckDisabled}
          >
            {t('metricAdd.confirmInput')}
          </Button>
        )
      }
      <Button
        block
        size="large"
        onClick={onCancel}
      >
        {t('common.cancel')}
      </Button>
    </Space>
  );
};

export default BottomButtons;
