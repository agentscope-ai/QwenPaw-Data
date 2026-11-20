/** Dashboard card shell — hairline border, micro-caps label row, optional
 *  DEMO chip (shown whenever a widget renders fallback/synthetic data). */
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import styles from '../../dashboard.module.less';

interface DashCardProps {
  title: string;
  /** Short category line above the title (same language as `title`). */
  microLabel: string;
  demo?: boolean;
  /** Extra content in the header row (filters, refresh…). */
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}

export default function DashCard({
  title,
  microLabel,
  demo = false,
  extra,
  children,
  className,
}: DashCardProps) {
  const { t } = useTranslation();
  return (
    <section className={`${styles.dashCard} ${className || ''}`}>
      <header className={styles.dashCardHead}>
        <div className={styles.dashCardTitles}>
          <span className={styles.dashMicro}>{microLabel}</span>
          <h3 className={styles.dashCardTitle}>{title}</h3>
        </div>
        <div className={styles.dashCardExtra}>
          {demo && <span className={styles.demoChip}>{t('graphCenter.dashboard.demoChip')}</span>}
          {extra}
        </div>
      </header>
      <div className={styles.dashCardBody}>{children}</div>
    </section>
  );
}
