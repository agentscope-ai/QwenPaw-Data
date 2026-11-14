import type { UIEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Card, Empty, Select, Skeleton } from 'antd';

import { useAuthStore } from '@/store/authStore';
import UserProfileCard from './UserProfileCard';
import MemoryTable from './MemoryTable';
import { useUserGraph } from './useUserGraph';
import type { UserGraphMemoryQuery } from './useUserGraph';
import styles from './index.module.less';

interface UserGraphPanelProps {
  initialTargetUserId?: string;
  initialMemoryQuery?: Partial<UserGraphMemoryQuery>;
}

/** Card-shaped loading placeholder matching the profile + memories layout. */
function PanelSkeleton() {
  const { t } = useTranslation();
  return (
    <>
      <Card title={t('kgBrowser.userGraph.profile')}>
        <Skeleton active title={false} paragraph={{ rows: 4 }} />
      </Card>
      <Card title={t('kgBrowser.userGraph.memories')}>
        <Skeleton active title={false} paragraph={{ rows: 6 }} />
      </Card>
    </>
  );
}

/** Explains why the panel has no data and what makes it appear. */
function PanelPlaceholder({
  title,
  hint,
  onRetry,
}: {
  title: string;
  hint: string;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className={styles.placeholder}>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <>
            <p className={styles.placeholderTitle}>{title}</p>
            <p className={styles.placeholderHint}>{hint}</p>
          </>
        }
      >
        {onRetry && <Button onClick={onRetry}>{t('common.refresh')}</Button>}
      </Empty>
    </div>
  );
}

export default function UserGraphPanel({
  initialTargetUserId,
  initialMemoryQuery,
}: UserGraphPanelProps) {
  const { t } = useTranslation();
  const authEnabled = useAuthStore((state) => state.authEnabled);
  const currentUserId = useAuthStore((state) => state.userId);
  const capabilities = useAuthStore((state) => state.capabilities);
  const isPlatformAdmin = useAuthStore((state) => state.isPlatformAdmin);
  const hasIdentity = authEnabled && Boolean(currentUserId);
  const canBrowseOthers = hasIdentity && capabilities.includes('tg_kg:others_read');
  const userGraph = useUserGraph({
    enabled: hasIdentity && !isPlatformAdmin,
    currentUserId,
    initialTargetUserId,
    initialMemoryQuery,
    canWriteOwn: hasIdentity && capabilities.includes('tg_kg:self'),
  });

  const ownerOptions = userGraph.owners.map((owner) => ({
    value: owner.user_id,
    label: owner.profile?.display_name
      ? `${owner.profile.display_name} (${owner.user_id})`
      : owner.user_id,
  }));

  const handleOwnerPopupScroll = (event: UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    if (target.scrollTop + target.clientHeight >= target.scrollHeight - 24) {
      userGraph.loadMoreOwners();
    }
  };

  // Nothing to show: tell the user which of the three reasons applies.
  const placeholder =
    !userGraph.targetUserId && canBrowseOthers
      ? {
          title: t('kgBrowser.userGraph.selectOwnerTitle'),
          hint: t('kgBrowser.userGraph.selectOwnerHint'),
        }
      : !userGraph.targetUserId
        ? {
            title: t('kgBrowser.userGraph.identityUnavailableTitle'),
            hint: t('kgBrowser.userGraph.identityUnavailableHint'),
          }
        : {
            title: t('kgBrowser.userGraph.notGeneratedTitle'),
            hint: t('kgBrowser.userGraph.notGeneratedHint'),
            onRetry: userGraph.retryTarget,
          };

  return (
    <section className={styles.panel} aria-label={t('kgBrowser.zone_ug')}>
      <header className={styles.header}>
        <div>
          <h2>{t('kgBrowser.zone_ug')}</h2>
          <p>{t('kgBrowser.zone_ug_desc')}</p>
        </div>
        {canBrowseOthers && !isPlatformAdmin && (
          <div className={styles.ownerSelect}>
            <span>{t('kgBrowser.userGraph.owner')}</span>
            <Select
              value={userGraph.targetUserId || undefined}
              options={ownerOptions}
              loading={userGraph.ownersLoading}
              placeholder={t('kgBrowser.userGraph.selectOwner')}
              onChange={userGraph.selectTargetUser}
              onPopupScroll={handleOwnerPopupScroll}
              showSearch
              optionFilterProp="label"
            />
          </div>
        )}
      </header>
      <div className={styles.content}>
        {isPlatformAdmin ? (
          <Alert
            type="warning"
            showIcon
            message={t('kgBrowser.userGraph.platformAdminUnavailable')}
          />
        ) : !hasIdentity ? (
          <PanelPlaceholder
            title={t('kgBrowser.userGraph.identityUnavailableTitle')}
            hint={t('kgBrowser.userGraph.identityUnavailableHint')}
          />
        ) : (
          <>
            {userGraph.ownersError && (
              <Alert
                className={styles.alert}
                type="warning"
                showIcon
                message={t('kgBrowser.userGraph.ownerLoadFailed')}
                description={userGraph.ownersError}
                action={
                  <Button size="small" onClick={userGraph.reloadOwners}>
                    {t('common.retry')}
                  </Button>
                }
              />
            )}
            {!userGraph.canWrite && userGraph.targetUserId && (
              <Alert
                className={styles.alert}
                type="info"
                showIcon
                message={t('kgBrowser.userGraph.readOnlyNotice')}
              />
            )}
            {userGraph.targetLoading ? (
              <PanelSkeleton />
            ) : userGraph.targetError ? (
              <Alert
                type="error"
                showIcon
                message={t('kgBrowser.userGraph.loadFailed')}
                description={userGraph.targetError}
                action={
                  <Button size="small" onClick={userGraph.retryTarget}>
                    {t('common.retry')}
                  </Button>
                }
              />
            ) : userGraph.user ? (
              <>
                <UserProfileCard
                  key={userGraph.user.user_id}
                  user={userGraph.user}
                  canWrite={userGraph.canWrite}
                  saving={userGraph.profileSaving}
                  onSave={userGraph.updateProfile}
                />
                <MemoryTable
                  key={`memories:${userGraph.user.user_id}`}
                  memories={userGraph.memories}
                  total={userGraph.memoriesMeta?.total ?? 0}
                  query={userGraph.memoryQuery}
                  loading={userGraph.memoriesLoading}
                  error={userGraph.memoriesError}
                  canWrite={userGraph.canWrite}
                  mutation={userGraph.memoryMutation}
                  onQueryChange={userGraph.updateMemoryQuery}
                  onPageChange={userGraph.changeMemoryPage}
                  onRefresh={userGraph.refreshMemories}
                  onLoad={userGraph.getMemory}
                  onCreate={userGraph.createMemory}
                  onUpdate={userGraph.updateMemory}
                  onRevoke={userGraph.revokeMemory}
                />
              </>
            ) : (
              <PanelPlaceholder {...placeholder} />
            )}
          </>
        )}
      </div>
    </section>
  );
}
