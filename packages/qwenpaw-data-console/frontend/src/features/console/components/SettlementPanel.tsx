import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Input, Tag, Tooltip } from '@agentscope-ai/design';
import { Empty, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { SettlementCard } from '../api/settlement';
import {
  getSettlementCardTitle,
  SETTLEMENT_TYPE_LABELS,
} from '../model/settlementCard';
import type { ConversationTurn } from '../types';
import styles from '../index.module.less';

const FIELD_LABELS: Record<string, string> = {
  metric_name: '指标名',
  caliber: '口径',
  formula_sql: '公式 SQL',
  table: '数据表',
  domain: '业务域',
  dimension_name: '维度名',
  bind_column: '绑定列',
  value_samples: '取值样例',
  column_name: '列名',
  meaning: '含义',
  use_case: '使用场景',
  recommended_dataset: '推荐数据集',
};

const FIELD_ORDER: Record<string, string[]> = {
  metric_caliber: ['metric_name', 'caliber', 'domain', 'table', 'formula_sql'],
  dimension_def: ['dimension_name', 'bind_column', 'value_samples', 'domain', 'table'],
  column_meaning: ['column_name', 'meaning', 'table', 'domain'],
  dataset_usage: ['use_case', 'recommended_dataset', 'domain'],
};

const EDITABLE_FIELDS = new Set([
  'caliber',
  'formula_sql',
  'meaning',
  'value_samples',
  'use_case',
  'recommended_dataset',
]);

const PROCESSED_STATUS_COPY: Record<string, { label: string; color?: string }> = {
  confirmed: { label: '已沉淀', color: 'success' },
  dismissed: { label: '已忽略' },
};

function orderedFieldEntries(card: SettlementCard, fields: Record<string, string>) {
  const preferredOrder = FIELD_ORDER[card.type] ?? [];
  const allKeys = [...preferredOrder, ...Object.keys(fields)];
  return [...new Set(allKeys)]
    .filter((key) => preferredOrder.includes(key) || String(fields[key] ?? '').trim())
    .map((key) => [key, fields[key] ?? ''] as const);
}

function getMissingFields(card: SettlementCard, fields: Record<string, string>): string[] {
  return (FIELD_ORDER[card.type] ?? Object.keys(card.fields)).filter(
    (key) => !String(fields[key] ?? '').trim(),
  );
}

function normalizeFields(fields: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(fields).map(([key, value]) => [key, String(value ?? '').trim()]),
  );
}

function sortCards(cards: SettlementCard[]): SettlementCard[] {
  return [...cards].sort((left, right) => (
    String(right.updated_at ?? right.created_at ?? '').localeCompare(
      String(left.updated_at ?? left.created_at ?? ''),
    )
  ));
}

interface SettlementPanelProps {
  sessionId: string | null | undefined;
  turns: ConversationTurn[];
  highlightKey: number;
  pendingCards: SettlementCard[];
  processedCards: SettlementCard[];
  pendingLoading: boolean;
  processedLoading: boolean;
  pendingError: string | null;
  processedError: string | null;
  actionableCount: number;
  onRefreshPending: () => void | Promise<void>;
  onRefreshProcessed: () => void | Promise<void>;
  onConfirm: (
    cardId: string,
    fields?: Record<string, string> | null,
  ) => Promise<void>;
  onDismiss: (cardId: string) => Promise<void>;
}

export default function SettlementPanel({
  sessionId,
  turns,
  highlightKey,
  pendingCards,
  processedCards,
  pendingLoading,
  processedLoading,
  pendingError,
  processedError,
  actionableCount,
  onRefreshPending,
  onRefreshProcessed,
  onConfirm,
  onDismiss,
}: SettlementPanelProps) {
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({});
  const [busyCardId, setBusyCardId] = useState<string | null>(null);
  const [expandedCardIds, setExpandedCardIds] = useState<string[]>([]);
  const [isHighlighted, setIsHighlighted] = useState(false);

  useEffect(() => {
    if (!highlightKey) return undefined;

    setIsHighlighted(true);
    const timeoutId = window.setTimeout(() => setIsHighlighted(false), 3200);
    return () => window.clearTimeout(timeoutId);
  }, [highlightKey]);

  useEffect(() => {
    setDrafts((current) => Object.fromEntries(
      pendingCards.map((card) => [card.id, current[card.id] ?? { ...card.fields }]),
    ));
  }, [pendingCards]);

  useEffect(() => {
    const cardIds = new Set(
      [...pendingCards, ...processedCards].map((card) => card.id),
    );
    setExpandedCardIds((current) => current.filter((cardId) => cardIds.has(cardId)));
  }, [pendingCards, processedCards]);

  const sortedPendingCards = useMemo(() => sortCards(pendingCards), [pendingCards]);
  const sortedProcessedCards = useMemo(() => sortCards(processedCards), [processedCards]);
  const turnNumbersById = useMemo(
    () => new Map(turns.map((turn) => [turn.id, turn.number])),
    [turns],
  );

  const updateField = (cardId: string, key: string, value: string) => {
    setDrafts((current) => ({
      ...current,
      [cardId]: {
        ...(current[cardId] ?? {}),
        [key]: value,
      },
    }));
  };

  const toggleCard = (cardId: string) => {
    setExpandedCardIds((current) => (
      current.includes(cardId)
        ? current.filter((currentCardId) => currentCardId !== cardId)
        : [...current, cardId]
    ));
  };

  const handleConfirm = async (card: SettlementCard) => {
    const draft = drafts[card.id] ?? card.fields;
    const missingFields = getMissingFields(card, draft);
    if (missingFields.length) {
      message.warning(`请补充：${missingFields.map((key) => FIELD_LABELS[key] || key).join('、')}`);
      return;
    }

    setBusyCardId(card.id);
    try {
      await onConfirm(card.id, normalizeFields(draft));
      message.success('已确认沉淀');
    } catch (confirmError) {
      message.error(
        confirmError instanceof Error ? confirmError.message : String(confirmError),
      );
    } finally {
      setBusyCardId(null);
    }
  };

  const handleDismiss = async (card: SettlementCard) => {
    setBusyCardId(card.id);
    try {
      await onDismiss(card.id);
      message.success('已忽略该推荐');
    } catch (dismissError) {
      message.error(
        dismissError instanceof Error ? dismissError.message : String(dismissError),
      );
    } finally {
      setBusyCardId(null);
    }
  };

  return (
    <div className={`${styles.workspacePanel} ${styles.workspaceSettlementPanel}`}>
      <section
        className={`${styles.workspaceContentSection} ${styles.settlementPanel} ${
          isHighlighted ? styles.settlementPanelHighlighted : ''
        }`}
      >
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>可沉淀项</strong>
            <span>
              {!sessionId
                ? '尚未选择会话'
                : pendingLoading && !sortedPendingCards.length
                  ? '检测中…'
                  : actionableCount
                    ? `${actionableCount} 条待确认`
                    : '对话结束后自动检测'}
            </span>
          </div>
          <Tooltip title="刷新可沉淀项">
            <Button
              type="text"
              size="small"
              icon={<ReloadOutlined />}
              loading={pendingLoading}
              disabled={!sessionId}
              aria-label="刷新可沉淀项"
              onClick={() => void onRefreshPending()}
            />
          </Tooltip>
        </div>

        {pendingError ? (
          <div className={styles.settlementError} role="alert">
            <span>{pendingError}</span>
            <Button type="link" size="small" onClick={() => void onRefreshPending()}>
              重试
            </Button>
          </div>
        ) : null}

        {sortedPendingCards.length ? (
          <div className={styles.settlementList}>
            {sortedPendingCards.map((card) => {
              const draft = drafts[card.id] ?? card.fields;
              const isBusy = busyCardId === card.id;
              const isExpanded = expandedCardIds.includes(card.id);
              const sourceTurnNumber = turnNumbersById.get(card.source_chat_id);

              return (
                <article className={styles.settlementCard} key={card.id}>
                  <header className={styles.settlementCardHeader}>
                    <button
                      className={styles.settlementCardToggle}
                      type="button"
                      aria-expanded={isExpanded}
                      aria-label={`${isExpanded ? '收起' : '展开'}${SETTLEMENT_TYPE_LABELS[card.type] || card.type}`}
                      onClick={() => toggleCard(card.id)}
                    >
                      <span className={styles.settlementCardToggleTop}>
                        <span className={styles.settlementCardTitleRow}>
                          <Tag bordered={false} color="processing" size="small">
                            {SETTLEMENT_TYPE_LABELS[card.type] || card.type}
                          </Tag>
                          {isExpanded ? (
                            <strong title={getSettlementCardTitle(card)}>
                              {getSettlementCardTitle(card)}
                            </strong>
                          ) : null}
                        </span>
                        <DownOutlined
                          className={isExpanded ? styles.settlementCardExpandIconOpen : undefined}
                        />
                      </span>
                      <span
                        className={styles.settlementCardMeta}
                        title={card.source_chat_id || undefined}
                      >
                        {sourceTurnNumber ? `来自 Chat ${sourceTurnNumber}` : '来自当前会话'}
                      </span>
                    </button>
                  </header>

                  {isExpanded ? (
                    <>
                      <div className={styles.settlementFields}>
                        {orderedFieldEntries(card, draft).map(([key, value]) => {
                          const isEditable = EDITABLE_FIELDS.has(key);
                          return (
                            <label className={styles.settlementField} key={key}>
                              <span>{FIELD_LABELS[key] || key}</span>
                              {isEditable ? (
                                <Input.TextArea
                                  autoSize={{ minRows: 2, maxRows: 7 }}
                                  value={value}
                                  disabled={isBusy}
                                  onChange={(event) => updateField(card.id, key, event.target.value)}
                                />
                              ) : (
                                <span className={styles.settlementFieldValue}>{value}</span>
                              )}
                            </label>
                          );
                        })}
                      </div>

                      <footer className={styles.settlementCardActions}>
                        <Button
                          size="small"
                          icon={<CloseOutlined />}
                          disabled={Boolean(busyCardId)}
                          onClick={() => void handleDismiss(card)}
                        >
                          忽略
                        </Button>
                        <Button
                          type="primary"
                          size="small"
                          icon={<CheckOutlined />}
                          loading={isBusy}
                          disabled={Boolean(busyCardId) && !isBusy}
                          onClick={() => void handleConfirm(card)}
                        >
                          确认沉淀
                        </Button>
                      </footer>
                    </>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : !pendingError ? (
          <SettlementEmpty
            title={!sessionId
              ? '尚未选择会话'
              : pendingLoading
                ? '正在检查可沉淀项'
                : '暂无待确认项'}
            description={!sessionId
              ? '打开一个会话后，可沉淀推荐会出现在这里。'
              : '对话完成后，系统会异步检测指标口径、维度定义、列含义和数据集用法。'}
          />
        ) : null}
      </section>

      <section className={`${styles.workspaceContentSection} ${styles.settlementPanel}`}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>已处理</strong>
            <span>
              {!sessionId
                ? '尚未选择会话'
                : processedLoading && !sortedProcessedCards.length
                  ? '加载中…'
                  : sortedProcessedCards.length
                    ? `${sortedProcessedCards.length} 条只读记录`
                    : '确认或忽略后出现在这里'}
            </span>
          </div>
        </div>

        {processedError ? (
          <div className={styles.settlementError} role="alert">
            <span>{processedError}</span>
            <Button type="link" size="small" onClick={() => void onRefreshProcessed()}>
              重试
            </Button>
          </div>
        ) : null}

        {sortedProcessedCards.length ? (
          <div className={styles.settlementList}>
            {sortedProcessedCards.map((card) => {
              const isExpanded = expandedCardIds.includes(card.id);
              const sourceTurnNumber = turnNumbersById.get(card.source_chat_id);
              const statusCopy = PROCESSED_STATUS_COPY[card.status]
                ?? { label: card.status };

              return (
                <article
                  className={`${styles.settlementCard} ${styles.settlementCardReadOnly}`}
                  key={card.id}
                >
                  <header className={styles.settlementCardHeader}>
                    <button
                      className={styles.settlementCardToggle}
                      type="button"
                      aria-expanded={isExpanded}
                      aria-label={`${isExpanded ? '收起' : '展开'}已处理${SETTLEMENT_TYPE_LABELS[card.type] || card.type}`}
                      onClick={() => toggleCard(card.id)}
                    >
                      <span className={styles.settlementCardToggleTop}>
                        <span className={styles.settlementCardTitleRow}>
                          <Tag bordered={false} color="processing" size="small">
                            {SETTLEMENT_TYPE_LABELS[card.type] || card.type}
                          </Tag>
                          <Tag bordered={false} color={statusCopy.color} size="small">
                            {statusCopy.label}
                          </Tag>
                          {isExpanded ? (
                            <strong title={getSettlementCardTitle(card)}>
                              {getSettlementCardTitle(card)}
                            </strong>
                          ) : null}
                        </span>
                        <DownOutlined
                          className={isExpanded ? styles.settlementCardExpandIconOpen : undefined}
                        />
                      </span>
                      <span
                        className={styles.settlementCardMeta}
                        title={card.source_chat_id || undefined}
                      >
                        {sourceTurnNumber ? `来自 Chat ${sourceTurnNumber}` : '来自当前会话'}
                      </span>
                    </button>
                  </header>

                  {isExpanded ? (
                    <div className={styles.settlementFields}>
                      {orderedFieldEntries(card, card.fields).map(([key, value]) => (
                        <div className={styles.settlementField} key={key}>
                          <span>{FIELD_LABELS[key] || key}</span>
                          <span className={styles.settlementFieldValue}>{value}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : !processedError ? (
          <SettlementEmpty
            title={!sessionId
              ? '尚未选择会话'
              : processedLoading
                ? '正在加载已处理项'
                : '暂无已处理项'}
            description={!sessionId
              ? '打开一个会话后，可查看该会话的已处理记录。'
              : '确认沉淀或忽略的推荐会以只读形式保留在这里。'}
          />
        ) : null}
      </section>
    </div>
  );
}

function SettlementEmpty({ title, description }: { title: string; description: string }) {
  return (
    <Empty
      className={styles.workspaceEmptyState}
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={(
        <span className={styles.workspaceEmptyDescription}>
          <strong>{title}</strong>
          <span>{description}</span>
        </span>
      )}
    />
  );
}
