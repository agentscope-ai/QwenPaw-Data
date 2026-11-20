import { useEffect } from 'react';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader/index';
import { useAppI18n } from '@/i18n';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import Content from './Content';
import styles from './index.module.less';

export default function SettlementCenterPage() {
  const { t } = useAppI18n();
  const { settlementController, consoleController } = useConsoleLayout();
  useEffect(() => {
    const previousTitle = document.title;
    document.title = `${t('settlementCenter.title')} · QwenPaw Data`;
    return () => {
      document.title = previousTitle;
    };
  }, [t]);
  return (
    <div className={styles.content}>
      <ConsoleRouteHeader
        title={t('settlementCenter.title')}
        description={t('settlementCenter.description')}
      />
      <main className={styles.main}>
        <div className={styles.wrap}>
          <Content
            key={settlementController.scope ?? 'unavailable'}
            controller={settlementController}
            sessions={consoleController.sessions}
          />
        </div>
      </main>
    </div>
  );
}
