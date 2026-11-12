/** Widget 7 · Hotspots — top-degree nodes with gradient degree bars.
 *  Row click jumps to the entity detail page. */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import type { GraphDb } from '../../dataset/graphDb';
import { typeLabel } from '../../dataset/graphDb';
import { deriveHotspots } from '../../dataset/dashboardData';
import UsageBar from './charts/UsageBar';
import DashCard from './DashCard';
import LayerGlyph from './LayerGlyph';
import styles from '../../dashboard.module.less';

export default function HotspotList({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const hotspots = useMemo(() => deriveHotspots(db, 8), [db]);
  const max = Math.max(...hotspots.map((h) => h.degree), 1);

  return (
    <DashCard
      title={t('graphCenter.dashboard.hotTitle')}
      microLabel={t('graphCenter.dashboard.hotMicro')}
    >
      {hotspots.length === 0 ? (
        <div className={styles.emptyBlock}>
          <span className={styles.dashedRing} />
          {t('graphCenter.dashboard.emptyGeneric')}
        </div>
      ) : (
        <ul className={styles.hotList}>
          {hotspots.map((h, i) => (
            <li key={h.node.id}>
              <button
                type="button"
                className={styles.hotRow}
                onClick={() => navigate(`/graph-center/entity/${encodeURIComponent(h.node.id)}`)}
              >
                <span className={styles.hotRank}>{String(i + 1).padStart(2, '0')}</span>
                <LayerGlyph layer={h.node.layer} size={12} />
                <span className={styles.hotName}>{h.node.label}</span>
                <span className={styles.hotType}>{typeLabel(h.node.type)}</span>
                <span className={styles.hotBar}>
                  <UsageBar value={h.degree} max={max} glow={h.degree / max >= 0.75} />
                </span>
                <span className={styles.hotCount}>{h.degree}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </DashCard>
  );
}
