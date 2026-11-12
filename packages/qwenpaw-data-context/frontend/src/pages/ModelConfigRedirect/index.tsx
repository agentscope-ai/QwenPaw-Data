import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Spin } from '@/design';
import { resolveHostModelConfigUrl } from '@/auth/config';

export default function ModelConfigRedirect() {
  const { t } = useTranslation();
  const target = resolveHostModelConfigUrl();
  useEffect(() => {
    if (target) window.location.replace(target);
  }, [target]);
  return target ? (
    <Spin />
  ) : (
    <Alert showIcon type="warning" message={t('modelConfig.hostUnavailable')} />
  );
}
