/** Widget 5 · Data health — glowing concentric rings (score + issue rings)
 *  with a micro-caps legend of the three issue classes. Legend rows jump to
 *  the compact issue list on this same page. */
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import type { GraphDb } from '../../dataset/graphDb';
import type { HealthIssueCode } from '../../types';
import { deriveHealth } from '../../dataset/dashboardData';
import { dashboardHealthPath } from './HealthIssueTable';
import RadialGauge from './charts/RadialGauge';
import DashCard from './DashCard';
import styles from '../../dashboard.module.less';

export default function HealthRings({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const h = deriveHealth(db);

  const ringColor = (n: number, warn: string, ok: string) => (n > 0 ? warn : ok);
  const legend: { n: number; label: string; code: HealthIssueCode }[] = [
    { n: h.missingDef, label: t('graphCenter.dashboard.healthMissingDef'), code: 'missingDef' },
    { n: h.unusedDim, label: t('graphCenter.dashboard.healthUnusedDim'), code: 'unusedDim' },
    { n: h.isolated, label: t('graphCenter.dashboard.healthIsolated'), code: 'isolated' },
  ];

  return (
    <DashCard
      title={t('graphCenter.dashboard.healthTitle')}
      microLabel={t('graphCenter.dashboard.healthMicro')}
    >
      <div className={styles.healthWrap}>
        <RadialGauge
          size={164}
          centerValue={`${h.score}`}
          centerLabel={t('graphCenter.dashboard.healthScore')}
          rings={[
            { fraction: h.score / 100, color: '#14B8A6', glow: '#14B8A6' },
            { fraction: h.missingDef > 0 ? Math.min(1, h.missingDef / 20) : 0, color: '#7C4DD6' },
            { fraction: h.unusedDim > 0 ? Math.min(1, h.unusedDim / 20) : 0, color: '#FAAD14' },
            {
              fraction: h.isolated > 0 ? Math.min(1, h.isolated / 10) : 0,
              color: ringColor(h.isolated, '#FF7A45', '#14B8A6'),
            },
          ]}
        />
        <div className={styles.healthLegend}>
          {legend.map((row) => (
            <button
              key={row.code}
              type="button"
              className={styles.healthLegendRow}
              onClick={() => navigate(dashboardHealthPath(row.code))}
            >
              <span className={`${styles.healthNum} ${row.n > 0 ? styles.healthNumWarn : ''}`}>
                {row.n}
              </span>
              <span className={styles.healthLegendLabel}>{row.label}</span>
            </button>
          ))}
        </div>
      </div>
    </DashCard>
  );
}
