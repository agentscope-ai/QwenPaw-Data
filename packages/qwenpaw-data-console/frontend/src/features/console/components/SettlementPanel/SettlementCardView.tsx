import { CheckOutlined, CloseOutlined, DownOutlined } from '@ant-design/icons';
import { Button, Input, Tag } from '@agentscope-ai/design';
import type { ReactNode } from 'react';
import type { SettlementCard } from '../../api/settlement';
import {
  getSettlementCardTitle,
  SETTLEMENT_TYPE_LABELS,
} from '../../model/settlement/settlementCard';
import {
  EDITABLE_FIELDS,
  FIELD_LABEL_KEYS,
  TYPE_LABEL_KEYS,
  orderedFieldEntries,
} from '../../model/settlement/settlementCardFields';
import { isPendingCard } from '../../model/settlement/settlementCenter';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

interface Props {
  card: SettlementCard;
  draftFields?: Record<string, string>;
  expanded: boolean;
  busy?: boolean;
  actionsDisabled?: boolean;
  sourceLabel?: string;
  timeLabel?: string;
  alwaysShowTitle?: boolean;
  footerStart?: ReactNode;
  onToggle: () => void;
  onFieldChange?: (key: string, value: string) => void;
  onConfirm?: () => void;
  onDismiss?: () => void;
}

export default function SettlementCardView({
  card,
  draftFields,
  expanded,
  busy = false,
  actionsDisabled = false,
  sourceLabel,
  timeLabel,
  alwaysShowTitle = false,
  footerStart,
  onToggle,
  onFieldChange,
  onConfirm,
  onDismiss,
}: Props) {
  const { t } = useAppI18n();
  const pending = isPendingCard(card);
  const typeLabel = TYPE_LABEL_KEYS[card.type] ? t(TYPE_LABEL_KEYS[card.type]) : card.type;
  const rawTitle = getSettlementCardTitle(card);
  const title = rawTitle === SETTLEMENT_TYPE_LABELS[card.type] ? typeLabel : rawTitle;
  return (
    <article className={`${styles.settlementCard} ${pending ? '' : styles.settlementCardReadOnly}`}>
      <header className={styles.settlementCardHeader}>
        <button
          className={styles.settlementCardToggle}
          type="button"
          aria-expanded={expanded}
          aria-label={`${expanded ? t('assets.collapse') : t('assets.expand')}${typeLabel}`}
          onClick={onToggle}
        >
          <span className={styles.settlementCardToggleTop}>
            <span className={styles.settlementCardTitleRow}>
              <Tag bordered={false} color="processing" size="small">
                {typeLabel}
              </Tag>
              {!pending ? (
                <Tag
                  bordered={false}
                  color={card.status === 'confirmed' ? 'success' : undefined}
                  size="small"
                >
                  {card.status === 'confirmed'
                    ? t('assets.statusConfirmed')
                    : card.status === 'dismissed'
                      ? t('assets.statusDismissed')
                      : card.status}
                </Tag>
              ) : null}
              {expanded || alwaysShowTitle ? <strong title={title}>{title}</strong> : null}
            </span>
            <DownOutlined className={expanded ? styles.settlementCardExpandIconOpen : undefined} />
          </span>
          {sourceLabel ? (
            <span className={styles.settlementCardMeta} title={card.source_chat_id || undefined}>
              {sourceLabel}
            </span>
          ) : null}
          {timeLabel ? <span className={styles.settlementCardMeta}>{timeLabel}</span> : null}
        </button>
      </header>
      {expanded ? (
        <>
          <div className={styles.settlementFields}>
            {orderedFieldEntries(card, draftFields ?? card.fields).map(([key, value]) => (
              <label className={styles.settlementField} key={key}>
                <span>{FIELD_LABEL_KEYS[key] ? t(FIELD_LABEL_KEYS[key]) : key}</span>
                {pending && onFieldChange && EDITABLE_FIELDS.has(key) ? (
                  <Input.TextArea
                    autoSize={{ minRows: 2, maxRows: 7 }}
                    value={value}
                    disabled={busy}
                    onChange={(event) => onFieldChange(key, event.target.value)}
                  />
                ) : (
                  <span className={styles.settlementFieldValue}>{value}</span>
                )}
              </label>
            ))}
          </div>
          {footerStart || (pending && onConfirm && onDismiss) ? (
            <footer className={styles.settlementCardActions}>
              {footerStart}
              {pending && onConfirm && onDismiss ? (
                <>
                  <Button
                    size="small"
                    icon={<CloseOutlined />}
                    disabled={actionsDisabled || busy}
                    onClick={onDismiss}
                  >
                    {t('assets.dismiss')}
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    icon={<CheckOutlined />}
                    loading={busy}
                    disabled={actionsDisabled && !busy}
                    onClick={onConfirm}
                  >
                    {t('assets.confirm')}
                  </Button>
                </>
              ) : null}
            </footer>
          ) : null}
        </>
      ) : null}
    </article>
  );
}
