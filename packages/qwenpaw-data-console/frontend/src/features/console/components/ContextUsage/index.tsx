import { CloseOutlined } from '@ant-design/icons';
import { Button, Tooltip } from '@agentscope-ai/design';
import { useId } from 'react';
import { useAppI18n } from '@/i18n';
import type { ContextUsage } from '../../api/types';
import {
  contextCompressionProgress,
  contextUsagePercent,
  formatContextTokens,
} from '../../model/runtime/contextUsage';
import styles from './index.module.less';

interface UsageProps {
  usage: ContextUsage | null;
}

export function ContextUsageButton({
  usage,
  open,
  onClick,
}: UsageProps & {
  open: boolean;
  onClick: () => void;
}) {
  const { t } = useAppI18n();
  const percent = usage ? contextCompressionProgress(usage) : null;
  const reached = usage ? usage.used_tokens >= usage.context_size * usage.trigger_ratio : false;
  const label =
    percent === null
      ? t('contextUsage.title')
      : t('contextUsage.buttonLabel', { percent: Math.round(percent) });
  return (
    <Tooltip title={label}>
      <button
        type="button"
        className={styles.toggle}
        data-open={open}
        data-reached={reached}
        aria-label={label}
        aria-expanded={open}
        onClick={onClick}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
          <circle className={styles.ringTrack} cx="12" cy="12" r="9" />
          {percent !== null ? (
            <circle
              className={styles.ringFill}
              cx="12"
              cy="12"
              r="9"
              pathLength="100"
              strokeDasharray={`${Math.min(100, percent)} 100`}
              transform="rotate(-90 12 12)"
            />
          ) : (
            <circle cx="12" cy="12" r="2" fill="currentColor" />
          )}
        </svg>
        {percent !== null ? <span>{Math.round(percent)}%</span> : null}
      </button>
    </Tooltip>
  );
}

/** Shows the latest model usage for the current session. */
export default function ContextUsagePanel({
  usage,
  onClose,
}: UsageProps & {
  onClose: () => void;
}) {
  const { t, language } = useAppI18n();
  const titleId = useId();
  const percent = usage ? contextUsagePercent(usage) : null;
  const reached = usage ? usage.used_tokens >= usage.context_size * usage.trigger_ratio : false;
  const formatExact = (value: number) =>
    value.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');

  return (
    <section
      className={styles.panel}
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <header className={styles.header}>
        <strong id={titleId}>{t('contextUsage.title')}</strong>
        <Button
          type="text"
          size="small"
          icon={<CloseOutlined />}
          aria-label={t('contextUsage.close')}
          onClick={onClose}
        />
      </header>
      <div className={styles.usageCard}>
        <div className={styles.summary}>
          <span className={styles.percent}>
            {percent === null ? '—' : `${Math.round(percent)}%`}{' '}
            <small>{t('contextUsage.used')}</small>
          </span>
          <span
            title={
              usage
                ? `${formatExact(usage.used_tokens)} / ${formatExact(usage.context_size)} Tokens`
                : undefined
            }
          >
            {usage
              ? `${formatContextTokens(usage.used_tokens)} / ${formatContextTokens(usage.context_size)} Tokens`
              : t('contextUsage.waiting')}
          </span>
        </div>
        <div className={styles.trackArea}>
          <div
            className={styles.track}
            data-reached={reached}
            role={usage ? 'progressbar' : undefined}
            aria-label={t('contextUsage.title')}
            aria-valuenow={percent === null ? undefined : Math.min(100, percent)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={
              usage
                ? `${formatExact(usage.used_tokens)} / ${formatExact(usage.context_size)} Tokens`
                : undefined
            }
          >
            <span className={styles.fill} style={{ width: `${Math.min(100, percent ?? 0)}%` }} />
          </div>
          {usage ? (
            <Tooltip
              title={t('contextUsage.triggerAt', {
                percent: Math.round(usage.trigger_ratio * 100),
                tokens: formatContextTokens(Math.floor(usage.context_size * usage.trigger_ratio)),
              })}
            >
              <span className={styles.marker} style={{ left: `${usage.trigger_ratio * 100}%` }} />
            </Tooltip>
          ) : null}
        </div>
        <div className={styles.usageNote}>
          <span>{t('contextUsage.lastSample')}</span>
          {usage ? (
            <span>
              {t('contextUsage.triggerAt', {
                percent: Math.round(usage.trigger_ratio * 100),
                tokens: formatContextTokens(Math.floor(usage.context_size * usage.trigger_ratio)),
              })}
            </span>
          ) : null}
        </div>
      </div>
    </section>
  );
}
