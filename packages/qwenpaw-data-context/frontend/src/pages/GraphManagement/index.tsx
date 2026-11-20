import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Spin, Tag, GlobalOutlined, TeamOutlined } from '@/design';
import { useAuthStore } from '@/store/authStore';
import OrgBreadcrumb from './OrgBreadcrumb';
import { graphCenterGlobalPath, switchWorkspaceAndGo, workspaceDetailPath } from './orgNav';
import styles from './index.module.less';

/** Graph management landing page.
 *  - tenant_admin: workspace list + global MG entry of the active workspace.
 *  - ws_admin: redirected to their active workspace detail (MG + members).
 *  - member / auth disabled: redirected to their own three-graph view. */
export default function GraphManagementPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const isTenantAdmin = useAuthStore((s) => s.isTenantAdmin);
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const tenantId = useAuthStore((s) => s.tenantId);
  const workspaces = useAuthStore((s) => s.workspaces);
  const activeWorkspaceId = useAuthStore((s) => s.activeWorkspaceId);
  const activeWorkspaceName = useAuthStore((s) => s.activeWorkspaceName);
  const [switchingId, setSwitchingId] = useState<string | null>(null);

  if (!authEnabled || !isAdmin) {
    return <Navigate to="/graph-center" replace />;
  }
  if (!isTenantAdmin) {
    return <Navigate to={workspaceDetailPath(activeWorkspaceId)} replace />;
  }

  const handleWorkspaceClick = async (workspaceId: string) => {
    if (switchingId) return;
    if (workspaceId === activeWorkspaceId) {
      navigate(workspaceDetailPath(workspaceId));
      return;
    }
    setSwitchingId(workspaceId);
    try {
      await switchWorkspaceAndGo(
        workspaceId,
        workspaceDetailPath(workspaceId),
        t('graphManagement.switchFailed'),
      );
    } catch {
      setSwitchingId(null);
    }
  };

  return (
    <div className={styles.page}>
      <OrgBreadcrumb
        items={[{ label: t('graphManagement.breadcrumb.tenant', { tenant: tenantId }) }]}
      />

      <div className={`${styles.section} context-page-surface`}>
        <h3 className={styles.sectionTitle}>{t('graphManagement.mgEntryTitle')}</h3>
        <p className={styles.sectionDesc}>{t('graphManagement.mgEntryDesc')}</p>
        <div className={styles.cardGrid}>
          <div className={styles.entryCard} onClick={() => navigate(graphCenterGlobalPath())}>
            <GlobalOutlined className={styles.entryCardIcon} />
            <div className={styles.entryCardBody}>
              <div className={styles.entryCardTitle}>{t('graphManagement.mgEntryCard')}</div>
              <div className={styles.entryCardMeta}>
                {t('graphManagement.mgEntryWorkspace', { workspace: activeWorkspaceName })}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className={`${styles.section} context-page-surface`}>
        <h3 className={styles.sectionTitle}>{t('graphManagement.workspaceListTitle')}</h3>
        <p className={styles.sectionDesc}>{t('graphManagement.workspaceListDesc')}</p>
        <div className={styles.cardGrid}>
          {workspaces.map((ws) => (
            <div
              key={ws.id}
              className={styles.entryCard}
              onClick={() => handleWorkspaceClick(ws.id)}
            >
              <TeamOutlined className={styles.entryCardIcon} />
              <div className={styles.entryCardBody}>
                <div className={styles.entryCardTitle}>{ws.name}</div>
                <div className={styles.entryCardMeta}>{ws.id}</div>
              </div>
              {switchingId === ws.id ? (
                <span className={styles.entryCardActiveTag}>
                  <Spin size="small" />
                </span>
              ) : ws.id === activeWorkspaceId ? (
                <Tag color="blue" className={styles.entryCardActiveTag}>
                  {t('graphManagement.currentWorkspace')}
                </Tag>
              ) : null}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
