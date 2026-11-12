/** Topic chip rail — pack as many topic chips as the leftover toolbar
 *  width allows; overflow goes into「⋯ 更多」so later toolbar actions
 *  stay on the same row. */
import { useLayoutEffect, useRef, useState } from 'react';
import { Dropdown } from 'antd';
import { useTranslation } from 'react-i18next';
import type { Topic } from '@/services/cmgraph';
import styles from '../index.module.less';

const GAP = 4;

/** Greedy prefix that still fits `budget`. Pins the active chip when it
 *  would otherwise fall into the overflow menu. */
export function pickVisibleTopicIndices(
  widths: number[],
  activeIndex: number,
  budget: number,
  moreWidth: number,
): number[] {
  const n = widths.length;
  if (n === 0) return [];

  const cost = (ids: number[]) => {
    if (ids.length === 0) return 0;
    return ids.reduce((sum, i) => sum + widths[i], 0) + GAP * (ids.length - 1);
  };

  const all = Array.from({ length: n }, (_, i) => i);
  if (cost(all) <= budget) return all;

  const limit = Math.max(0, budget - moreWidth - GAP);

  const withActive = (prefixLen: number): number[] => {
    if (activeIndex >= 0 && activeIndex >= prefixLen) {
      const keep = Math.max(0, prefixLen - 1);
      return [...Array.from({ length: keep }, (_, i) => i), activeIndex];
    }
    return Array.from({ length: prefixLen }, (_, i) => i);
  };

  for (let k = n - 1; k >= 1; k--) {
    const ids = withActive(k);
    if (cost(ids) <= limit) return ids;
  }

  if (activeIndex >= 0) return [activeIndex];
  return [0];
}

function sameIds(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

export default function TopicChipRail({
  topics,
  activeTopicId,
  topicColorOf,
  fallbackColor,
  onToggle,
}: {
  topics: Topic[];
  activeTopicId: string;
  topicColorOf: Map<string, string>;
  fallbackColor: string;
  onToggle: (id: string) => void;
}) {
  const { t } = useTranslation();
  const railRef = useRef<HTMLSpanElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [visibleIds, setVisibleIds] = useState<string[]>(() => topics.map((tp) => tp.id));

  const colorOf = (id: string) => topicColorOf.get(id) || fallbackColor;

  const renderChip = (tp: Topic) => {
    const c = colorOf(tp.id);
    const on = activeTopicId === tp.id;
    return (
      <span
        key={tp.id}
        className={`${styles.topicChip} ${on ? styles.topicChipActive : ''}`}
        style={on ? { borderColor: c, color: c, background: `${c}14` } : undefined}
        title={tp.summary || tp.title}
        onClick={() => onToggle(tp.id)}
      >
        <i className={styles.topicDot} style={{ background: c }} />
        <span className={styles.topicChipTitle}>{tp.title}</span>
        <span className={styles.topicCount}>{tp.size}</span>
        {tp.cross_layer === false && (
          <span className={styles.topicBadgeSingle}>{t('graphCenter.topicsSingleLayerBadge')}</span>
        )}
      </span>
    );
  };

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
      const activeIndex = topics.findIndex((tp) => tp.id === activeTopicId);
      const idxs = pickVisibleTopicIndices(
        widths,
        activeIndex,
        rail.clientWidth,
        moreEl.offsetWidth,
      );
      const next = idxs.map((i) => topics[i].id);
      setVisibleIds((prev) => (sameIds(prev, next) ? prev : next));
    };

    apply();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(apply);
    ro.observe(rail);
    return () => ro.disconnect();
  }, [topics, activeTopicId, t]);

  const visible = visibleIds
    .map((id) => topics.find((tp) => tp.id === id))
    .filter((tp): tp is Topic => Boolean(tp));
  const hidden = topics.filter((tp) => !visibleIds.includes(tp.id));

  return (
    <span className={styles.topicChipRail} ref={railRef}>
      <span className={styles.topicChipMeasure} ref={measureRef} aria-hidden>
        {topics.map((tp) => renderChip(tp))}
        <span className={styles.topicChip}>
          ⋯ {t('graphCenter.topicsMore', { num: topics.length })}
        </span>
      </span>
      {visible.map((tp) => renderChip(tp))}
      {hidden.length > 0 && (
        <Dropdown
          trigger={['click']}
          menu={{
            items: hidden.map((tp) => ({
              key: tp.id,
              label: (
                <span>
                  <i
                    style={{
                      display: 'inline-block',
                      width: 7,
                      height: 7,
                      borderRadius: '50%',
                      background: colorOf(tp.id),
                      marginRight: 6,
                    }}
                  />
                  {tp.title} <span style={{ color: '#bfbfbf' }}>{tp.size}</span>
                  {tp.cross_layer === false && (
                    <span className={styles.topicBadgeSingle} style={{ marginLeft: 4 }}>
                      {t('graphCenter.topicsSingleLayerBadge')}
                    </span>
                  )}
                </span>
              ),
              onClick: () => onToggle(tp.id),
            })),
          }}
        >
          <span className={styles.topicChip} title={t('graphCenter.topicsMoreTip')}>
            ⋯ {t('graphCenter.topicsMore', { num: hidden.length })}
          </span>
        </Dropdown>
      )}
    </span>
  );
}
