/** TopicCard — right-side lens panel for one discovered topic.
 *  Lists the topic's members grouped by layer (MG/TG/KG/UG facets) with
 *  layer glyphs; clicking a member navigates to entity/trace detail. */
import { useMemo } from 'react';
import { Button } from '@/design';
import type { Topic } from '@/services/cmgraph';
import type { LayerId } from '../types';
import { LAYER_COLOR } from '../dataset/constants';
import LayerGlyph from './dashboard/LayerGlyph';
import styles from './TopicCard.module.less';

const LAYERS = ['MG', 'KG', 'UG'] as const;
const LAYER_NAME: Record<LayerId, string> = {
  MG: '元数据',
  TG: '轨迹',
  KG: '知识',
  UG: '用户',
};

interface TopicCardProps {
  topic: Topic;
  color: string;
  /** id of the member node in the current dataset ("" = not in scope). */
  resolveId: (key: string) => string;
  onMemberClick: (nodeId: string, layer: LayerId, type: string) => void;
  onExit: () => void;
}

export default function TopicCard({
  topic,
  color,
  resolveId,
  onMemberClick,
  onExit,
}: TopicCardProps) {
  const facets = useMemo(() => {
    const by: Partial<Record<LayerId, Topic['members']>> = {};
    topic.members.forEach((m) => {
      (by[m.layer] = by[m.layer] || []).push(m);
    });
    return LAYERS.map((L) => ({
      layer: L,
      items: (by[L] || []).slice(0, 24),
      total: (by[L] || []).length,
    })).filter((f) => f.items.length > 0);
  }, [topic]);

  return (
    <aside className={styles.card}>
      <div className={styles.head} style={{ borderColor: color }}>
        <span className={styles.topicTag} style={{ background: color }}>
          TOPIC
        </span>
        <div className={styles.title}>{topic.title}</div>
        {topic.summary && <div className={styles.summary}>{topic.summary}</div>}
        <div className={styles.meta}>
          {LAYERS.map((L) =>
            topic.layer_counts[L] ? (
              <span key={L} className={styles.metaItem}>
                <LayerGlyph layer={L} size={11} /> {L} · {topic.layer_counts[L]}
              </span>
            ) : null,
          )}
        </div>
      </div>

      <div className={styles.body}>
        {facets.map((f) => (
          <div key={f.layer} className={styles.facet}>
            <div className={styles.facetHead}>
              <LayerGlyph layer={f.layer} size={13} />
              <span style={{ color: LAYER_COLOR[f.layer] }}>{LAYER_NAME[f.layer]}</span>
              <span className={styles.facetCount}>{f.total}</span>
            </div>
            <ul className={styles.memberList}>
              {f.items.map((m) => {
                const id = resolveId(m.key);
                const clickable = !!id;
                return (
                  <li
                    key={m.key}
                    className={clickable ? styles.member : `${styles.member} ${styles.memberOff}`}
                    title={m.key}
                    onClick={() => clickable && onMemberClick(id, f.layer, m.type)}
                  >
                    <span className={styles.memberLabel}>{m.label}</span>
                    <span className={styles.memberType}>{m.type}</span>
                  </li>
                );
              })}
              {f.total > f.items.length && (
                <li className={styles.more}>… 还有 {f.total - f.items.length} 项</li>
              )}
            </ul>
          </div>
        ))}
      </div>

      <div className={styles.foot}>
        <Button size="small" onClick={onExit}>
          退出主题
        </Button>
      </div>
    </aside>
  );
}
