/** Widget 3 · Four-layer node distribution as a nightingale rose. */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { LAYER_COLOR } from '../../dataset/constants';
import type { GraphDb } from '../../dataset/graphDb';
import { deriveLayerDistribution } from '../../dataset/dashboardData';
import RoseChart from './charts/RoseChart';
import DashCard from './DashCard';
import styles from '../../dashboard.module.less';

export default function LayerRoseChart({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const dist = useMemo(() => deriveLayerDistribution(db), [db]);
  const total = dist.reduce((a, d) => a + d.nodes, 0);

  return (
    <DashCard
      title={t('graphCenter.dashboard.layerTitle')}
      microLabel={t('graphCenter.dashboard.layerMicro')}
    >
      {total === 0 ? (
        <div className={styles.emptyBlock}>
          <span className={styles.dashedRing} />
          {t('graphCenter.dashboard.emptyGeneric')}
        </div>
      ) : (
        <div className={styles.roseWrap}>
          <RoseChart
            items={dist.map((d) => ({
              label: t(
                `graphCenter.dashboard.layerShort${d.layer[0]}${d.layer.slice(1).toLowerCase()}`,
              ),
              value: d.nodes,
              color: LAYER_COLOR[d.layer],
              caption: `${d.nodes}`,
            }))}
            size={196}
            centerValue={total}
            centerLabel={t('graphCenter.dashboard.layerCenter')}
          />
          <ul className={styles.roseLegend}>
            {dist.map((d) => (
              <li key={d.layer}>
                <span
                  className={styles.legendSwatch}
                  style={{ background: LAYER_COLOR[d.layer] }}
                />
                <span className={styles.roseLegendName}>
                  {t(`graphCenter.legend.${d.layer.toLowerCase()}`)}
                </span>
                <span className={styles.roseLegendNum}>
                  {t('graphCenter.dashboard.layerStat', { nodes: d.nodes, edges: d.edges })}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </DashCard>
  );
}
