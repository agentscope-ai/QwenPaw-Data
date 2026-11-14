/** Widget 1 · KPI strip — four thin-line stat cards with gradient underlines. */
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { deriveKpis } from '../../dataset/dashboardData';
import type { GraphDb } from '../../dataset/graphDb';
import { dashboardHealthPath } from './HealthIssueTable';
import styles from '../../dashboard.module.less';

export default function KpiStrip({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const k = deriveKpis(db);
  const items = [
    {
      key: 'nodes',
      label: t('graphCenter.dashboard.kpiNodes'),
      micro: t('graphCenter.dashboard.kpiMicroNodes'),
      value: k.nodeCount,
    },
    {
      key: 'edges',
      label: t('graphCenter.dashboard.kpiEdges'),
      micro: t('graphCenter.dashboard.kpiMicroEdges'),
      value: k.edgeCount,
    },
    {
      key: 'issues',
      label: t('graphCenter.dashboard.kpiIssues'),
      micro: t('graphCenter.dashboard.kpiMicroIssues'),
      value: k.issueCount,
      warn: k.issueCount > 0,
      onClick: () => navigate(dashboardHealthPath()),
    },
  ];
  return (
    <div className={styles.kpiRow}>
      {items.map((item) => {
        const body = (
          <>
            <span className={styles.dashMicro}>{item.micro}</span>
            <div className={`${styles.kpiNum} ${item.warn ? styles.kpiNumWarn : ''}`}>
              {item.value.toLocaleString()}
            </div>
            <div className={styles.kpiLabel}>{item.label}</div>
            <div className={styles.kpiLine} />
          </>
        );
        return item.onClick ? (
          <button
            key={item.key}
            type="button"
            className={`${styles.kpiCard} ${styles.kpiCardClick}`}
            onClick={item.onClick}
          >
            {body}
          </button>
        ) : (
          <div key={item.key} className={styles.kpiCard}>
            {body}
          </div>
        );
      })}
    </div>
  );
}
