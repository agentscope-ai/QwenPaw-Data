/** Widget 8 · Recent audit feed — vertical hairline timeline with layer-hued
 *  dots and letter-spaced timestamps. No real audit endpoint exists yet
 *  (v2 follow-up: GET /api/v1/admin/audit/recent), so API mode shows the
 *  honest empty state; only the clearly-labeled offline demo mode renders
 *  the dataset-derived demo feed. */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { LAYER_COLOR } from '../../dataset/constants';
import type { GraphDb } from '../../dataset/graphDb';
import { deriveDemoAudit, type AuditDemoKind } from '../../dataset/dashboardData';
import DashCard from './DashCard';
import styles from '../../dashboard.module.less';

const AUDIT_COPY: Record<AuditDemoKind, { title: string; detail: string }> = {
  trace: {
    title: 'graphCenter.dashboard.auditDemoTrace',
    detail: 'graphCenter.dashboard.auditDemoTraceDetail',
  },
  claim: {
    title: 'graphCenter.dashboard.auditDemoClaim',
    detail: 'graphCenter.dashboard.auditDemoClaimDetail',
  },
  sync: {
    title: 'graphCenter.dashboard.auditDemoSync',
    detail: 'graphCenter.dashboard.auditDemoSyncDetail',
  },
  tool: {
    title: 'graphCenter.dashboard.auditDemoTool',
    detail: 'graphCenter.dashboard.auditDemoToolDetail',
  },
  userspace: {
    title: 'graphCenter.dashboard.auditDemoUserspace',
    detail: 'graphCenter.dashboard.auditDemoUserspaceDetail',
  },
  memory: {
    title: 'graphCenter.dashboard.auditDemoMemory',
    detail: 'graphCenter.dashboard.auditDemoMemoryDetail',
  },
};

export default function AuditFeed({ db, demo }: { db: GraphDb; demo: boolean }) {
  const { t } = useTranslation();
  // API mode: never fabricate audit events — show the empty state until the
  // real v2 endpoint lands. Demo mode: clearly-labeled synthetic feed.
  const events = useMemo(() => (demo ? deriveDemoAudit(db, 8) : []), [db, demo]);

  return (
    <DashCard
      title={t('graphCenter.dashboard.auditTitle')}
      microLabel={t('graphCenter.dashboard.auditMicro')}
      demo={demo}
    >
      {events.length === 0 ? (
        <div className={styles.emptyBlock}>
          <span className={styles.dashedRing} />
          {t('graphCenter.dashboard.auditEmpty')}
        </div>
      ) : (
        <ul className={styles.auditList}>
          {events.map((ev) => (
            <li key={ev.id} className={styles.auditRow}>
              <span className={styles.auditDot} style={{ background: LAYER_COLOR[ev.layer] }} />
              <div className={styles.auditMain}>
                <div className={styles.auditTitle}>{t(AUDIT_COPY[ev.kind].title)}</div>
                <div className={styles.auditDetail}>
                  {t(AUDIT_COPY[ev.kind].detail, { num: ev.count ?? 0 })}
                </div>
              </div>
              <span className={styles.auditTime}>{ev.time}</span>
            </li>
          ))}
        </ul>
      )}
    </DashCard>
  );
}
