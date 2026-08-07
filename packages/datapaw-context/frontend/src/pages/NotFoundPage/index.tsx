import { Button, Result } from '@/design';
import React from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';


const NotFoundPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <Result
      status="404"
      title="404"
      subTitle={t('empty.notFoundSubtitle')}
      extra={
        <Button type="primary" onClick={() => navigate('/')}>
          {t('common.back')}
        </Button>
      }
    />
  )
}

export default NotFoundPage;
