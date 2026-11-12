/** Datasource chip rail — fold overflow into「⋯ 更多」so the topic
 *  lens still has room for its own more-button. */
import { useLayoutEffect, useRef, useState } from 'react';
import { Dropdown } from 'antd';
import { useTranslation } from 'react-i18next';
import { pickVisibleTopicIndices } from './TopicChipRail';
import styles from '../index.module.less';

function sameIds(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

export default function DatasourceChipRail({
  items,
  activeId,
  onSelect,
}: {
  items: { id: string; label: string }[];
  activeId: string;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const railRef = useRef<HTMLSpanElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [visibleIds, setVisibleIds] = useState<string[]>(() => items.map((it) => it.id));

  const renderChip = (it: { id: string; label: string }) => (
    <span
      key={it.id}
      className={`${styles.chip} ${it.id === activeId ? styles.chipActive : ''}`}
      title={it.label}
      onClick={() => onSelect(it.id)}
    >
      {it.label}
    </span>
  );

  useLayoutEffect(() => {
    const rail = railRef.current;
    const measure = measureRef.current;
    if (!rail || !measure) return;

    const apply = () => {
      const kids = Array.from(measure.children) as HTMLElement[];
      if (kids.length < 2) return;
      const moreEl = kids[kids.length - 1];
      const chipEls = kids.slice(0, -1);
      const widths = chipEls.map((el) => el.offsetWidth);
      const activeIndex = items.findIndex((it) => it.id === activeId);
      const idxs = pickVisibleTopicIndices(
        widths,
        activeIndex,
        rail.clientWidth,
        moreEl.offsetWidth,
      );
      const next = idxs.map((i) => items[i].id);
      setVisibleIds((prev) => (sameIds(prev, next) ? prev : next));
    };

    apply();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(apply);
    ro.observe(rail);
    return () => ro.disconnect();
  }, [items, activeId, t]);

  const visible = visibleIds
    .map((id) => items.find((it) => it.id === id))
    .filter((it): it is { id: string; label: string } => Boolean(it));
  const hidden = items.filter((it) => !visibleIds.includes(it.id));

  return (
    <span className={styles.dsChipRail} ref={railRef}>
      <span className={styles.dsChipMeasure} ref={measureRef} aria-hidden>
        {items.map((it) => renderChip(it))}
        <span className={styles.chip}>⋯ {t('graphCenter.dsMore', { num: items.length })}</span>
      </span>
      {visible.map((it) => renderChip(it))}
      {hidden.length > 0 && (
        <Dropdown
          trigger={['click']}
          menu={{
            items: hidden.map((it) => ({
              key: it.id,
              label: it.label,
              onClick: () => onSelect(it.id),
            })),
          }}
        >
          <span className={styles.chip} title={t('graphCenter.dsMoreTip')}>
            ⋯ {t('graphCenter.dsMore', { num: hidden.length })}
          </span>
        </Dropdown>
      )}
    </span>
  );
}
