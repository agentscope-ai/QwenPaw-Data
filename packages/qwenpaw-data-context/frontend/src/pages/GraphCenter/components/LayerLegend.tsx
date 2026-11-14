import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { usePresence } from './iosMotion';
import styles from '../index.module.less';

/** Supported layer legend rows (hexagon / rounded square / circle). */
export function LegendContent() {
  const { t } = useTranslation();
  const rows = [
    { layer: 'MG', cls: styles.lgMg, label: t('graphCenter.legend.mg') },
    { layer: 'KG', cls: styles.lgKg, label: t('graphCenter.legend.kg') },
    { layer: 'UG', cls: styles.lgUg, label: t('graphCenter.legend.ug') },
  ];
  return (
    <div className={styles.legendBox}>
      <div className={styles.legendTitle}>{t('graphCenter.legend.title')}</div>
      {rows.map((r) => (
        <div key={r.layer} className={styles.legendRow}>
          <span className={`${styles.lgShape} ${r.cls}`} />
          {r.label}
        </div>
      ))}
    </div>
  );
}

/** Tiny three-dot mark previewing the layer palette. */
function LegendDots() {
  return (
    <span className={styles.legendDots} aria-hidden="true">
      <i className={styles.lgMg} />
      <i className={styles.lgKg} />
      <i className={styles.lgUg} />
    </span>
  );
}

interface LayerLegendPopoverProps {
  className?: string;
}

/** Compact "图例" trigger that expands into an iOS-style floating card. */
export default function LayerLegendPopover({ className }: LayerLegendPopoverProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const { mounted, entered } = usePresence(open);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`${styles.legendWrap} ${className || ''}`}>
      <button
        type="button"
        className={`${styles.legendTrigger} ${open ? styles.legendTriggerOpen : ''}`}
        aria-expanded={open}
        aria-label={t('graphCenter.legendToggle')}
        onClick={() => setOpen((value) => !value)}
      >
        <LegendDots />
        {t('graphCenter.legendToggle')}
      </button>
      {mounted ? (
        <div className={`${styles.legendFloat} ${entered ? styles.legendFloatOpen : ''}`}>
          <LegendContent />
        </div>
      ) : null}
    </div>
  );
}
