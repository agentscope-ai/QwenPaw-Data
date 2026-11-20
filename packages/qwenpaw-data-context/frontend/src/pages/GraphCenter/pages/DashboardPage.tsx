/** Dashboard · 数据总览 — Graph Center operational overview.
 *  Overview widgets plus the compact health-issue list (formerly /audit).
 *  Consumes the shell-level GraphCenterContext so the dataset is loaded once
 *  for all tabs and the admin drilldown stays shared. */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Tooltip } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useGraphCenter } from '../GraphCenterContext';
import KpiStrip from '../components/dashboard/KpiStrip';
import LayerRoseChart from '../components/dashboard/LayerRoseChart';
import CrossLayerRings from '../components/dashboard/CrossLayerRings';
import HealthRings from '../components/dashboard/HealthRings';
import SyncStatusCard from '../components/dashboard/SyncStatusCard';
import HotspotList from '../components/dashboard/HotspotList';
import AuditFeed from '../components/dashboard/AuditFeed';
import CuratedAssets from '../components/dashboard/CuratedAssets';
import HealthIssueTable from '../components/dashboard/HealthIssueTable';
import FadePresence from '../components/FadePresence';
import styles from '../dashboard.module.less';

export default function DashboardPage() {
  const { t } = useTranslation();
  const { scope, db, loading, error, source, reload } = useGraphCenter();
  const demo = source === 'demo';
  const workspaceId = scope.workspaceId;

  // Prefer the session's human-readable workspace name (matches the header
  // switcher) over the raw id stamped into the dataset meta.
  const workspaceLabel = useMemo(
    () =>
      scope.workspaceName ||
      db.dataset.meta.workspace ||
      workspaceId ||
      t('graphCenter.dashboard.workspaceFallback'),
    [scope.workspaceName, db, workspaceId, t],
  );
  const firstLoad = loading && db.dataset.meta.source === 'pending';
  const refreshing = loading && !firstLoad;
  const spaceMessage =
    error === 'user_space_uninitialized'
      ? t('kgBrowser.mgSync.uninitialized')
      : error === 'user_space_initializing'
        ? t('kgBrowser.mgSync.initializing')
        : null;

  return (
    <div className={styles.dashPage}>
      <header className={styles.dashHero}>
        <span className={styles.dashHeroMeta}>{workspaceLabel}</span>
        <div className={styles.dashHeroRight}>
          {demo && <span className={styles.demoChipLg}>{t('graphCenter.dashboard.demoChip')}</span>}
          {error && !demo && (
            <span className={styles.errorNote}>
              {spaceMessage ?? t('graphCenter.dashboard.loadError')}
            </span>
          )}
          <Tooltip
            title={
              loading ? t('graphCenter.dashboard.loading') : t('graphCenter.dashboard.refresh')
            }
          >
            <Button
              type="text"
              shape="circle"
              className={styles.refreshBtn}
              icon={<ReloadOutlined spin={loading} />}
              onClick={reload}
              disabled={loading}
              aria-label={t('graphCenter.dashboard.refresh')}
            />
          </Tooltip>
        </div>
      </header>

      {firstLoad ? (
        <FadePresence open kind="page" className={styles.loadingPage}>
          {t('graphCenter.dashboard.loading')}
        </FadePresence>
      ) : error && !demo ? (
        <FadePresence open kind="page" className={styles.loadingPage}>
          <span>{spaceMessage ?? t('graphCenter.usageFailed')}</span>
          <Button type="primary" onClick={reload}>
            {t('graphCenter.usageRetry')}
          </Button>
        </FadePresence>
      ) : (
        <div className={`${styles.dashGrid} ${refreshing ? styles.dashGridDim : ''}`}>
          <div className={styles.span12}>
            <KpiStrip db={db} />
          </div>
          <div className={styles.span12}>
            <LayerRoseChart db={db} />
          </div>
          <div className={styles.span5}>
            <CrossLayerRings db={db} />
          </div>
          <div className={styles.span4}>
            <HealthRings db={db} />
          </div>
          <div className={styles.span3}>
            <SyncStatusCard db={db} demo={demo} />
          </div>
          <div className={styles.span7}>
            <HotspotList db={db} />
          </div>
          <div className={styles.span5}>
            <AuditFeed db={db} demo={demo} />
          </div>
          <div className={styles.span12}>
            <HealthIssueTable db={db} />
          </div>
          <div className={styles.span12}>
            <CuratedAssets db={db} demo={demo} />
          </div>
        </div>
      )}
    </div>
  );
}
