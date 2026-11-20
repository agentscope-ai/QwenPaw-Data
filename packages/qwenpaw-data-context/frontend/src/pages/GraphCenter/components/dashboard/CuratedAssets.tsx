/** Recently curated MG metrics and dimensions. */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import type { GraphDb } from '../../dataset/graphDb';
import { deriveCuratedAssets } from '../../dataset/dashboardData';
import DashCard from './DashCard';
import LayerGlyph from './LayerGlyph';
import styles from '../../dashboard.module.less';

type KindFilter = 'all' | 'metric' | 'dimension';

export default function CuratedAssets({ db, demo }: { db: GraphDb; demo: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [kind, setKind] = useState<KindFilter>('all');

  const assets = useMemo(() => deriveCuratedAssets(db, 8), [db]);
  const filtered = kind === 'all' ? assets : assets.filter((a) => a.kind === kind);

  const filters: { key: KindFilter; label: string }[] = [
    { key: 'all', label: t('graphCenter.dashboard.caAll') },
    { key: 'metric', label: t('graphCenter.dashboard.caMetric') },
    { key: 'dimension', label: t('graphCenter.dashboard.caDimension') },
  ];

  return (
    <DashCard
      title={t('graphCenter.dashboard.caTitle')}
      microLabel={t('graphCenter.dashboard.caMicro')}
      demo={demo}
      extra={
        <div className={styles.caFilter}>
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`${styles.caFilterBtn} ${kind === f.key ? styles.caFilterBtnActive : ''}`}
              onClick={() => setKind(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
      }
    >
      {filtered.length === 0 ? (
        <div className={styles.emptyBlock}>
          <span className={styles.dashedRing} />
          {t('graphCenter.dashboard.caEmpty')}
        </div>
      ) : (
        <ul className={styles.caList}>
          {filtered.map((a) => (
            <li key={a.node.id}>
              <button
                type="button"
                className={styles.caRow}
                onClick={() => navigate(`/graph-center/entity/${encodeURIComponent(a.node.id)}`)}
              >
                <LayerGlyph layer="MG" size={14} />
                <div className={styles.caIdentity}>
                  <span className={styles.caName}>{a.node.label}</span>
                  <span
                    className={`${styles.caKind} ${a.kind === 'metric' ? styles.caKindMetric : styles.caKindDim}`}
                  >
                    {a.kind === 'metric'
                      ? t('graphCenter.dashboard.caMetric')
                      : t('graphCenter.dashboard.caDimension')}
                  </span>
                  {a.node.domain && <span className={styles.caDomain}>{a.node.domain}</span>}
                </div>
                <span className={styles.caDate}>
                  {a.createdAt ? `${t('graphCenter.dashboard.caCurated')} ${a.createdAt}` : '—'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </DashCard>
  );
}
