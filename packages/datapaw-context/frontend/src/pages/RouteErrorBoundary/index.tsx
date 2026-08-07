import { Result, Button } from '@/design';
import React from 'react';
import { useRouteError, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

const RouteErrorBoundary: React.FC = () => {
  const { t } = useTranslation();
  const error = useRouteError();
  const navigate = useNavigate();

  console.error('路由错误:', error);

  return (
    <Result
      status="error"
      title={t('empty.routeErrorTitle')}
      subTitle={t('empty.routeErrorSubtitle')}
      extra={[
        <Button key="retry" type="primary" onClick={() => window.location.reload()}>
          {t('empty.reload')}
        </Button>,
        <Button key="home" onClick={() => navigate('/')}>
          {t('empty.backHome')}
        </Button>,
      ]}
    />
  );
};

export default RouteErrorBoundary;
