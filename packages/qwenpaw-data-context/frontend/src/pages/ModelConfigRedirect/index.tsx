import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { isEmbedded } from '../../embedded/context';
import { useTranslation } from 'react-i18next';
import { Alert, Spin } from '@/design';
import { resolveHostModelConfigUrl } from '@/auth/config';

export default function ModelConfigRedirect() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const target = resolveHostModelConfigUrl();
  useEffect(() => {
    if (target) {
      if (isEmbedded()) navigate(target, { replace: true });
      else window.location.replace(target);
    }
  }, [target, navigate]);
  return target ? (
    <Spin />
  ) : (
    <Alert showIcon type="warning" message={t('modelConfig.hostUnavailable')} />
  );
}
