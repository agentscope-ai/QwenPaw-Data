import { ReloadOutlined } from '@ant-design/icons';
import { Button, Tooltip } from '@agentscope-ai/design';
import { Empty, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { SettlementCard } from '../../api/settlement';
import type { SkillDraft } from '../../api/skillDrafts';
import type { SkillProposal, SkillProposalAcceptSelection } from '../../api/skillProposals';
import type { ConversationTurn } from '../../types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import SkillProposalSection from './SkillProposalSection';
import SkillDraftPanel from '../SkillDraftPanel';
import ownerStyles from './index.module.less';
import SettlementCardView from './SettlementCardView';

import {
  FIELD_LABEL_KEYS,
  getMissingFields,
  normalizeFields,
} from '../../model/settlement/settlementCardFields';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

function sortCards(cards: SettlementCard[]): SettlementCard[] {
  return [...cards].sort((left, right) =>
    String(right.updated_at ?? right.created_at ?? '').localeCompare(
      String(left.updated_at ?? left.created_at ?? ''),
    ),
  );
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
  onConfirm: (cardId: string, fields?: Record<string, string> | null) => Promise<void>;
  onDismiss: (cardId: string) => Promise<void>;
  skillDrafts: SkillDraft[];
  skillDraftLoading: boolean;
  skillDraftError: string | null;
  onRefreshSkillDrafts: () => Promise<void>;
  skillPending: SkillProposal[];
  skillLoading: boolean;
  skillError: string | null;
  onRefreshSkill: () => void | Promise<void>;
  onAcceptSkill: (
    proposalId: string,
    selection?: SkillProposalAcceptSelection,
  ) => Promise<SkillProposal | undefined>;
  onRejectSkill: (proposalId: string) => Promise<SkillProposal | undefined>;
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
  skillDrafts,
  skillDraftLoading,
  skillDraftError,
  onRefreshSkillDrafts,
  skillPending,
  skillLoading,
  skillError,
  onRefreshSkill,
  onAcceptSkill,
  onRejectSkill,
}: SettlementPanelProps) {
  const { t } = useAppI18n();
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({});
  const [busyCardId, setBusyCardId] = useState<string | null>(null);
  const [expandedCardIds, setExpandedCardIds] = useState<string[]>([]);
  const [isHighlighted, setIsHighlighted] = useState(false);
  const showSkillDrafts = skillDrafts.length > 0 || skillDraftLoading || Boolean(skillDraftError);

  useEffect(() => {
    if (!highlightKey) return undefined;

    setIsHighlighted(true);
    const timeoutId = window.setTimeout(() => setIsHighlighted(false), 3200);
    return () => window.clearTimeout(timeoutId);
  }, [highlightKey]);

  useEffect(() => {
    setDrafts((current) =>
      Object.fromEntries(
        pendingCards.map((card) => [card.id, current[card.id] ?? { ...card.fields }]),
      ),
    );
  }, [pendingCards]);

  useEffect(() => {
    const cardIds = new Set([...pendingCards, ...processedCards].map((card) => card.id));
    setExpandedCardIds((current) => current.filter((cardId) => cardIds.has(cardId)));
  }, [pendingCards, processedCards]);

  const sortedPendingCards = useMemo(() => sortCards(pendingCards), [pendingCards]);
  const sortedProcessedCards = useMemo(() => sortCards(processedCards), [processedCards]);
  const turnNumbersById = useMemo(
    () => new Map(turns.map((turn) => [turn.id, turn.number])),
    [turns],
  );
  const fieldLabel = (key: string) => (FIELD_LABEL_KEYS[key] ? t(FIELD_LABEL_KEYS[key]) : key);
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
    setExpandedCardIds((current) =>
      current.includes(cardId)
        ? current.filter((currentCardId) => currentCardId !== cardId)
        : [...current, cardId],
    );
  };

  const handleConfirm = async (card: SettlementCard) => {
    const draft = drafts[card.id] ?? card.fields;
    const missingFields = getMissingFields(card, draft);
    if (missingFields.length) {
      message.warning(
        t('assets.missingFields', {
          fields: missingFields.map(fieldLabel).join('、'),
        }),
      );
      return;
    }

    setBusyCardId(card.id);
    try {
      await onConfirm(card.id, normalizeFields(draft));
      message.success(t('assets.confirmedSuccess'));
    } catch (confirmError) {
      message.error(confirmError instanceof Error ? confirmError.message : String(confirmError));
    } finally {
      setBusyCardId(null);
    }
  };

  const handleDismiss = async (card: SettlementCard) => {
    setBusyCardId(card.id);
    try {
      await onDismiss(card.id);
      message.success(t('assets.dismissedSuccess'));
    } catch (dismissError) {
      message.error(dismissError instanceof Error ? dismissError.message : String(dismissError));
    } finally {
      setBusyCardId(null);
    }
  };

  return (
    <div
      className={`${styles.workspacePanel} ${styles.workspaceSettlementPanel} ${
        showSkillDrafts ? styles.workspaceSettlementPanelWithDrafts : ''
      }`}
    >
      {showSkillDrafts ? (
        <SkillDraftPanel
          sessionId={sessionId}
          drafts={skillDrafts}
          loading={skillDraftLoading}
          error={skillDraftError}
          turns={turns}
          onRefresh={onRefreshSkillDrafts}
        />
      ) : null}
      <SkillProposalSection
        sessionId={sessionId}
        pending={skillPending}
        loading={skillLoading}
        error={skillError}
        onRefresh={onRefreshSkill}
        onAccept={onAcceptSkill}
        onReject={onRejectSkill}
      />
      <section
        className={`${styles.workspaceContentSection} ${styles.settlementPanel} ${
          isHighlighted ? styles.settlementPanelHighlighted : ''
        }`}
      >
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>{t('assets.pendingTitle')}</strong>
            <span>
              {!sessionId
                ? t('assets.noSession')
                : pendingLoading && !sortedPendingCards.length
                  ? t('assets.detecting')
                  : actionableCount
                    ? t('assets.pendingCount', { count: actionableCount })
                    : t('assets.autoDetect')}
            </span>
          </div>
          <Tooltip title={t('assets.refresh')}>
            <Button
              type="text"
              size="small"
              icon={<ReloadOutlined />}
              loading={pendingLoading}
              disabled={!sessionId}
              aria-label={t('assets.refresh')}
              onClick={() => void onRefreshPending()}
            />
          </Tooltip>
        </div>

        {pendingError ? (
          <div className={styles.settlementError} role="alert">
            <span>{pendingError}</span>
            <Button type="link" size="small" onClick={() => void onRefreshPending()}>
              {t('common.retry')}
            </Button>
          </div>
        ) : null}

        {sortedPendingCards.length ? (
          <div className={styles.settlementList}>
            {sortedPendingCards.map((card) => {
              const sourceTurnNumber = turnNumbersById.get(card.source_chat_id);
              return (
                <SettlementCardView
                  key={card.id}
                  card={card}
                  expanded={expandedCardIds.includes(card.id)}
                  onToggle={() => toggleCard(card.id)}
                  sourceLabel={
                    sourceTurnNumber
                      ? t('assets.fromChat', { number: sourceTurnNumber })
                      : t('assets.fromCurrentSession')
                  }
                  draftFields={drafts[card.id] ?? card.fields}
                  busy={busyCardId === card.id}
                  actionsDisabled={Boolean(busyCardId)}
                  onFieldChange={(key, value) => updateField(card.id, key, value)}
                  onConfirm={() => void handleConfirm(card)}
                  onDismiss={() => void handleDismiss(card)}
                />
              );
            })}
          </div>
        ) : !pendingError ? (
          <SettlementEmpty
            title={
              !sessionId
                ? t('assets.noSession')
                : pendingLoading
                  ? t('assets.checking')
                  : t('assets.noPending')
            }
            description={
              !sessionId ? t('assets.pendingEmptyNoSession') : t('assets.pendingEmptyDescription')
            }
          />
        ) : null}
      </section>

      <section className={`${styles.workspaceContentSection} ${styles.settlementPanel}`}>
        <div className={styles.workspaceSectionHeader}>
          <div>
            <strong>{t('assets.processed')}</strong>
            <span>
              {!sessionId
                ? t('assets.noSession')
                : processedLoading && !sortedProcessedCards.length
                  ? t('assets.loading')
                  : sortedProcessedCards.length
                    ? t('assets.processedCount', { count: sortedProcessedCards.length })
                    : t('assets.processedHint')}
            </span>
          </div>
        </div>

        {processedError ? (
          <div className={styles.settlementError} role="alert">
            <span>{processedError}</span>
            <Button type="link" size="small" onClick={() => void onRefreshProcessed()}>
              {t('common.retry')}
            </Button>
          </div>
        ) : null}

        {sortedProcessedCards.length ? (
          <div className={styles.settlementList}>
            {sortedProcessedCards.map((card) => {
              const sourceTurnNumber = turnNumbersById.get(card.source_chat_id);
              return (
                <SettlementCardView
                  key={card.id}
                  card={card}
                  expanded={expandedCardIds.includes(card.id)}
                  onToggle={() => toggleCard(card.id)}
                  sourceLabel={
                    sourceTurnNumber
                      ? t('assets.fromChat', { number: sourceTurnNumber })
                      : t('assets.fromCurrentSession')
                  }
                />
              );
            })}
          </div>
        ) : !processedError ? (
          <SettlementEmpty
            title={
              !sessionId
                ? t('assets.noSession')
                : processedLoading
                  ? t('assets.loadingProcessed')
                  : t('assets.noProcessed')
            }
            description={
              !sessionId
                ? t('assets.processedEmptyNoSession')
                : t('assets.processedEmptyDescription')
            }
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
      description={
        <span className={styles.workspaceEmptyDescription}>
          <strong>{title}</strong>
          <span>{description}</span>
        </span>
      }
    />
  );
}
