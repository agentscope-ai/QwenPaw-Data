import type { ReactNode } from 'react';
import { Alert, Button, Empty, Segmented, Skeleton } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ConsoleRouteHeader from '@/features/console/components/ConsoleRouteHeader';
import type { UsagePeriod } from '@/features/console/api/usageTypes';
import type { UsageError, UsageRegion } from '@/features/console/model/usage/usageController';
import { useAppI18n } from '@/i18n';
import { useUsage } from './useUsage';
import UsageTrend from './UsageTrend';
import UsageDetails from './UsageDetails';
import UsageValue from './UsageValue';
import styles from './index.module.less';

function Region({
  name,
  title,
  description,
  actions,
  region,
  blocked,
  onRetry,
  children,
}: {
  name: string;
  title: string;
  description: string;
  actions?: ReactNode;
  region: UsageRegion<unknown>;
  blocked: 'loading' | 'error' | null;
  onRetry: () => void;
  children?: ReactNode;
}) {
  const { t } = useAppI18n();
  const error: UsageError | null = region.error ?? (blocked === 'error' ? 'load' : null);
  const waiting = !region.data && !error && (region.loading || blocked === 'loading');
  return (
    <section
      className={styles.section}
      aria-labelledby={`usage-${name}`}
      aria-busy={waiting || region.loading}
    >
      <div className={styles.sectionHeader}>
        <div className={styles.heading}>
          <h2 id={`usage-${name}`}>{title}</h2>
          <p>{description}</p>
        </div>
        {actions}
      </div>
      {error ? (
        <Alert
          className={styles.regionAlert}
          type="error"
          showIcon
          message={t(`usage.error.${error}`)}
          action={
            <Button size="small" onClick={onRetry} loading={region.loading}>
              {t('usage.retry')}
            </Button>
          }
        />
      ) : null}
      {waiting ? <Skeleton active paragraph={{ rows: 3 }} /> : children}
      {!waiting && !error && !region.data ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<Button onClick={onRetry}>{t('usage.retry')}</Button>}
        />
      ) : null}
    </section>
  );
}

export default function UsagePage() {
  const { t } = useAppI18n();
  const { state, period, setPeriod, refresh, load } = useUsage();
  const summary = state.summary.data;
  const blocked = summary ? null : state.summary.error ? 'error' : 'loading';
  const metrics = summary
    ? [
        { label: t('usage.totalTokens'), value: summary.total_tokens },
        { label: t('usage.generationTokens'), value: summary.generation.total_tokens },
        { label: t('usage.embeddingTokens'), value: summary.embedding.tokens },
        { label: t('usage.generationCalls'), value: summary.generation.calls, compact: false },
        { label: t('usage.embeddingCalls'), value: summary.embedding.calls, compact: false },
      ]
    : [];
  return (
    <div className={styles.content}>
      <ConsoleRouteHeader title={t('settings.usage')} />
      <main className={styles.main}>
        <div className={styles.wrap}>
          <Region
            name="overview"
            title={t('usage.overview.title')}
            description={t('usage.overview.description')}
            region={state.summary}
            blocked={blocked}
            onRetry={() => void refresh()}
            actions={
              <div className={styles.controls}>
                <Segmented
                  value={period}
                  aria-label={t('usage.period')}
                  options={(['1d', '7d', '30d'] as const).map((value) => ({
                    value,
                    label: t(`usage.period.${value}`),
                  }))}
                  onChange={(value) => setPeriod(value as UsagePeriod)}
                />
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => void refresh()}
                  loading={state.summary.loading}
                  disabled={!state.ready}
                  aria-label={t('usage.refresh')}
                  title={t('usage.refresh')}
                />
              </div>
            }
          >
            {summary ? (
              <>
                <div className={styles.metrics}>
                  {metrics.map((metric) => (
                    <div className={styles.metric} key={metric.label}>
                      <span>{metric.label}</span>
                      <strong>
                        <UsageValue value={metric.value} compact={metric.compact} />
                      </strong>
                    </div>
                  ))}
                </div>
              </>
            ) : null}
          </Region>
          <Region
            name="trend"
            title={t('usage.trend.title')}
            description={t('usage.trend.description')}
            region={state.trend}
            blocked={blocked}
            onRetry={() => void (summary ? load('trend') : refresh())}
          >
            {state.trend.data ? (
              <UsageTrend key={state.trend.data.window.as_of} data={state.trend.data} />
            ) : null}
          </Region>
          <Region
            name="sessions"
            title={t('usage.sessions.title')}
            description={t('usage.sessions.description')}
            region={state.sessions}
            blocked={blocked}
            onRetry={() => void (summary ? load('sessions') : refresh())}
          >
            {state.sessions.data ? (
              <UsageDetails
                kind="sessions"
                data={state.sessions.data}
                page={state.sessions.data.pagination.page}
                loading={state.sessions.loading || state.summary.loading}
                onPageChange={(page) => void load('sessions', page)}
              />
            ) : null}
          </Region>
          <Region
            name="background"
            title={t('usage.background.title')}
            description={t('usage.background.description')}
            region={state.background}
            blocked={blocked}
            onRetry={() => void (summary ? load('background') : refresh())}
          >
            {state.background.data ? (
              <UsageDetails
                kind="background"
                data={state.background.data}
                page={state.background.data.pagination.page}
                loading={state.background.loading || state.summary.loading}
                onPageChange={(page) => void load('background', page)}
              />
            ) : null}
          </Region>
        </div>
      </main>
    </div>
  );
}
