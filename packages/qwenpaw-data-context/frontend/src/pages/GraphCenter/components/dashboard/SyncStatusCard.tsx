/** Widget 6 · MG sync status — pulsing gradient dot + digest/last-sync line.
 *  Real source: GET /api/v1/mg/userspace/sync-status. Every tenant role
 *  (member / ws_admin / tenant_admin) owns a personal userspace MG mirror.
 *  Should a session ever lack the capability, the card degrades to a
 *  global-MG overview instead of probing the endpoint. */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/store/authStore';
import type { GraphDb } from '../../dataset/graphDb';
import DashCard from './DashCard';
import { fetchMgSyncStatus, type MgSyncStatus } from './dashApi';
import styles from '../../dashboard.module.less';

export default function SyncStatusCard({ db, demo }: { db: GraphDb; demo: boolean }) {
  const { t } = useTranslation();
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const capabilities = useAuthStore((s) => s.capabilities);
  // Defensive: every tenant role has a userspace, but never hard-probe
  // without the capability.
  const hasUserspace = !authEnabled || capabilities.includes('mg_userspace:sync');
  const [status, setStatus] = useState<MgSyncStatus | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!hasUserspace) {
      setLoaded(true);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      void fetchMgSyncStatus().then((s) => {
        if (cancelled) return;
        setStatus(s);
        setLoaded(true);
        if (s?.space_status === 'uninitialized' || s?.space_status === 'initializing') {
          timer = setTimeout(load, 6000);
        }
      });
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [hasUserspace]);

  const mgCount = useMemo(() => db.dataset.nodes.filter((n) => n.layer === 'MG').length, [db]);

  const synced = status ? !status.needs_sync : true;
  const lastSync = status?.last_synced_at
    ? status.last_synced_at.slice(0, 16).replace('T', ' ')
    : '—';

  return (
    <DashCard
      title={t('graphCenter.dashboard.syncTitle')}
      microLabel={t('graphCenter.dashboard.syncMicro')}
      demo={demo}
    >
      <div className={styles.syncWrap}>
        <div className={styles.syncHead}>
          <span className={`${styles.syncDot} ${synced ? styles.syncDotOk : styles.syncDotWarn}`} />
          <span className={styles.syncState}>
            {status?.space_status === 'uninitialized' || status?.space_status === 'initializing'
              ? t(`kgBrowser.mgSync.${status.space_status}`)
              : synced
                ? t('graphCenter.dashboard.syncOk')
                : t('graphCenter.dashboard.syncDrift')}
          </span>
        </div>

        {!hasUserspace ? (
          <div className={styles.syncRows}>
            <div className={styles.syncRow}>
              <span className={styles.dashMicro}>{t('graphCenter.dashboard.syncGlobalMg')}</span>
              <span className={styles.syncVal}>{mgCount}</span>
            </div>
            <div className={styles.syncRow}>
              <span className={styles.dashMicro}>{t('graphCenter.dashboard.syncRole')}</span>
              <span className={styles.syncVal}>
                {authEnabled
                  ? t('graphCenter.dashboard.syncRoleGlobal')
                  : t('graphCenter.dashboard.syncRoleLocal')}
              </span>
            </div>
          </div>
        ) : !loaded ? (
          <div className={styles.loadingBlock}>{t('graphCenter.dashboard.loading')}</div>
        ) : (
          <div className={styles.syncRows}>
            <div className={styles.syncRow}>
              <span className={styles.dashMicro}>{t('graphCenter.dashboard.syncLast')}</span>
              <span className={styles.syncVal}>{lastSync}</span>
            </div>
            <div className={styles.syncRow}>
              <span className={styles.dashMicro}>{t('graphCenter.dashboard.syncDigest')}</span>
              <span className={`${styles.syncVal} ${styles.syncDigest}`}>
                {status?.global_digest ? status.global_digest.slice(0, 12) : '—'}
              </span>
            </div>
          </div>
        )}

        <div className={styles.syncStrip} aria-hidden>
          {[0, 1, 2, 3, 4].map((i) => (
            <span
              key={i}
              className={`${styles.syncTick} ${synced || i < 3 ? styles.syncTickOk : styles.syncTickWarn}`}
            />
          ))}
        </div>
      </div>
    </DashCard>
  );
}
