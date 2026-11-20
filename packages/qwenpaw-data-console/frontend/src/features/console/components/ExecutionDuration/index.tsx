import type { ExecutionTiming } from '../../api/types';
import { durationSeconds, formatExecutionSeconds } from '../../model/runtime/executionTiming';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

export default function ExecutionDuration({ timing }: { timing?: ExecutionTiming }) {
  const { t } = useAppI18n();
  if (!timing) return null;
  const seconds = durationSeconds(timing);
  const label = t('runtime.durationSeconds', {
    seconds: formatExecutionSeconds(seconds),
  });
  return (
    <span
      className={styles.duration}
      data-slow={timing.kind === 'tool' && seconds > 30 ? 'true' : undefined}
    >
      {label}
    </span>
  );
}
