import { CloseCircleFilled } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { useAppI18n } from '../../../../i18n';
import { buildAgentConfigurationPath } from '../../../../shared/navigation/sessionNavigation';
import type { selectResponseFailure } from '../../model/runtime/responseFailure';
import styles from './ResponseFailure.module.less';

export default function ResponseFailure({
  failure,
  readOnly,
}: {
  failure: NonNullable<ReturnType<typeof selectResponseFailure>>;
  readOnly: boolean;
}) {
  const { t } = useAppI18n();
  return (
    <div className={styles.failure}>
      <div className={styles.card} role="alert">
        <div className={styles.title}>
          <CloseCircleFilled className={styles.icon} aria-hidden="true" />
          <strong>{t('runtime.responseFailed')}</strong>
        </div>
        <div className={styles.message}>
          {failure.message || t('runtime.responseFailureFallback')}
        </div>
      </div>
      {failure.canConfigure && !readOnly ? (
        <Link className={styles.action} to={buildAgentConfigurationPath()}>
          {t('runtime.configureAgent')}
        </Link>
      ) : null}
    </div>
  );
}
