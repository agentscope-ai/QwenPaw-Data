import { useEffect, useMemo, useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Empty, Input, Spin, Tag, GlobalOutlined } from '@/design';
import { useAuthStore } from '@/store/authStore';
import OrgBreadcrumb from './OrgBreadcrumb';
import type { OrgCrumb } from './OrgBreadcrumb';
import { useWorkspaceMembers } from '@/hooks/useWorkspaceMembers';
import {
  GRAPH_MANAGEMENT_PATH,
  graphCenterGlobalPath,
  switchWorkspaceAndGo,
  workspaceDetailPath,
} from './orgNav';
import styles from './index.module.less';

/** Workspace detail: global MG entry + member list of one workspace. */
export default function WorkspaceDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { workspaceId = '' } = useParams();
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const isTenantAdmin = useAuthStore((s) => s.isTenantAdmin);
  const tenantId = useAuthStore((s) => s.tenantId);
  const workspaces = useAuthStore((s) => s.workspaces);
  const activeWorkspaceId = useAuthStore((s) => s.activeWorkspaceId);
  const currentUserId = useAuthStore((s) => s.userId);

  const isActiveWorkspace = workspaceId === activeWorkspaceId;
  const needsSwitch = authEnabled && isAdmin && workspaceId !== '' && !isActiveWorkspace;
  const [switchFailed, setSwitchFailed] = useState(false);

  // Physically isolated workspaces: browsing another workspace requires a
  // session switch + full reload; the URL restores the drill-down position.
  useEffect(() => {
    if (!needsSwitch || switchFailed) return;
    switchWorkspaceAndGo(
      workspaceId,
      workspaceDetailPath(workspaceId),
      t('graphManagement.switchFailed'),
    ).catch(() => setSwitchFailed(true));
  }, [needsSwitch, switchFailed, workspaceId, t]);

  const [memberSearch, setMemberSearch] = useState('');
  const { members, loading, error } = useWorkspaceMembers(
    isActiveWorkspace ? workspaceId : undefined,
  );

  const workspaceName = useMemo(
    () => workspaces.find((ws) => ws.id === workspaceId)?.name || workspaceId,
    [workspaces, workspaceId],
  );

  const filteredMembers = useMemo(
    () =>
      members.filter(
        (m) =>
          !memberSearch ||
          m.displayName.toLowerCase().includes(memberSearch.toLowerCase()) ||
          m.username.toLowerCase().includes(memberSearch.toLowerCase()),
      ),
    [members, memberSearch],
  );

  if (!authEnabled || !isAdmin) {
    return <Navigate to="/graph-center" replace />;
  }

  if (needsSwitch) {
    return (
      <div className={styles.switching}>
        {switchFailed ? (
          <Empty description={t('graphManagement.switchFailed')} />
        ) : (
          <>
            <Spin size="large" />
            <span>{t('graphManagement.switchingWorkspace', { workspace: workspaceName })}</span>
          </>
        )}
      </div>
    );
  }

  const crumbs: OrgCrumb[] = isTenantAdmin
    ? [
        {
          label: t('graphManagement.breadcrumb.tenant', { tenant: tenantId }),
          path: GRAPH_MANAGEMENT_PATH,
        },
        { label: workspaceName },
      ]
    : [{ label: workspaceName }];

  return (
    <div className={styles.page}>
      <OrgBreadcrumb items={crumbs} />

      <div className={`${styles.section} context-page-surface`}>
        <h3 className={styles.sectionTitle}>{t('graphManagement.mgEntryTitle')}</h3>
        <p className={styles.sectionDesc}>{t('graphManagement.mgEntryDesc')}</p>
        <div className={styles.cardGrid}>
          <div className={styles.entryCard} onClick={() => navigate(graphCenterGlobalPath())}>
            <GlobalOutlined className={styles.entryCardIcon} />
            <div className={styles.entryCardBody}>
              <div className={styles.entryCardTitle}>{t('graphManagement.mgEntryCard')}</div>
              <div className={styles.entryCardMeta}>
                {t('graphManagement.mgEntryWorkspace', { workspace: workspaceName })}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className={`${styles.section} context-page-surface`}>
        <h3 className={styles.sectionTitle}>
          {t('graphManagement.memberListTitle')}
          {!loading && ` (${members.length})`}
        </h3>
        <p className={styles.sectionDesc}>{t('graphManagement.memberListDesc')}</p>
        <Input
          className={styles.memberSearch}
          placeholder={t('kgBrowser.searchMember')}
          value={memberSearch}
          onChange={(e) => setMemberSearch(e.target.value)}
          size="small"
          allowClear
        />
        {loading ? (
          <div className={styles.listState}>
            <Spin size="small" />
          </div>
        ) : error ? (
          <div className={styles.listState}>{t('graphManagement.memberListFailed')}</div>
        ) : filteredMembers.length === 0 ? (
          <Empty
            description={t('graphManagement.memberListEmpty')}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        ) : (
          <div className={styles.memberList}>
            {filteredMembers.map((m) => (
              <div
                key={m.id}
                className={styles.memberItem}
                onClick={() => navigate(graphCenterGlobalPath({ memberId: m.id }))}
              >
                <span className={styles.memberAvatar}>{m.displayName.slice(0, 1)}</span>
                <span className={styles.memberName}>
                  {m.displayName}
                  {m.id === currentUserId && ` (${t('graphManagement.me')})`}
                </span>
                {m.role === 'ws_admin' && (
                  <Tag className={styles.memberRole}>{t('graphManagement.roleWsAdmin')}</Tag>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
