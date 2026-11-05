import { MenuOutlined } from '@ant-design/icons';
import { Button } from '@agentscope-ai/design';
import { LiquidGlass } from '@liquidglass/react';
import ConsoleSettingsMenu from '@/features/console/components/ConsoleSettingsMenu';
import { useConsoleLayout } from '@/layouts/ConsoleLayout/context';
import styles from './ConsoleRouteHeader.module.less';

interface ConsoleRouteHeaderProps {
  parentLabel: string;
  title: string;
  onParentClick: () => void;
}

export default function ConsoleRouteHeader({
  parentLabel,
  title,
  onParentClick,
}: ConsoleRouteHeaderProps) {
  const { openMobileNav } = useConsoleLayout();

  return (
    <LiquidGlass
      borderRadius={0}
      blur={10}
      contrast={1.08}
      brightness={1.06}
      saturation={1.24}
      shadowIntensity={0}
      displacementScale={0.42}
      elasticity={0.38}
      zIndex={4}
      className={styles.glass}
    >
      <header className={styles.header}>
        <div className={styles.headerRow}>
          <Button
            className={styles.mobileMenu}
            type="text"
            icon={<MenuOutlined />}
            aria-label="打开导航"
            onClick={openMobileNav}
          />
          <div>
            <div className={styles.breadcrumb}>
              <button type="button" onClick={onParentClick}>{parentLabel}</button>
              <span>/</span>
              <strong>{title}</strong>
            </div>
            <h1>{title}</h1>
          </div>
        </div>
        <ConsoleSettingsMenu />
      </header>
    </LiquidGlass>
  );
}
