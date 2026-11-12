import { MenuOutlined } from '@ant-design/icons';
import { Button } from '@agentscope-ai/design';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

interface ConsoleRouteHeaderProps {
  title: string;
  description?: string;
}

export default function ConsoleRouteHeader({ title, description }: ConsoleRouteHeaderProps) {
  const { openMobileNav } = useConsoleLayout();
  const { t } = useAppI18n();

  return (
    <header className={styles.header}>
      <div className={styles.headerRow}>
        <Button
          className={styles.mobileMenu}
          type="text"
          icon={<MenuOutlined />}
          aria-label={t('nav.open')}
          onClick={openMobileNav}
        />
        <div className={styles.headerContent}>
          <h1>{title}</h1>
          {description ? (
            <span className={styles.description} title={description}>
              {description}
            </span>
          ) : null}
        </div>
      </div>
    </header>
  );
}
