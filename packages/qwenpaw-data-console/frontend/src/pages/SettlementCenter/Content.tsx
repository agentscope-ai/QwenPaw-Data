import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import { Alert, Empty, Segmented, Spin, message } from 'antd';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import SettlementCardView from '@/features/console/components/SettlementPanel/SettlementCardView';
import SkillProposalCard from '@/features/console/components/SettlementPanel/SkillProposalCard';
import SkillProposalReviewModal from '@/features/console/components/SettlementPanel/SkillProposalReviewModal';
import type { SettlementCard } from '@/features/console/api/settlement';
import type { ConsoleSession } from '@/features/console/api/types';
import {
  skillProposalApi,
  type SkillProposal,
  type SkillProposalAcceptSelection,
} from '@/features/console/api/skillProposals';
import {
  collectSkillHunkIds,
  getSkillProposalFileChanges,
} from '@/features/console/model/settlement/skillProposal';
import {
  FIELD_LABEL_KEYS,
  getMissingFields,
  normalizeFields,
} from '@/features/console/model/settlement/settlementCardFields';
import {
  isPendingCard,
  selectSettlementItems,
  settlementItemKey,
  settlementTimestamp,
  type SettlementView,
} from '@/features/console/model/settlement/settlementCenter';
import type { SettlementListState } from '@/features/console/model/settlement/settlementCenterStore';
import type { SettlementCenterController } from '@/features/console/model/settlement/useSettlementCenter';
import { useAppI18n } from '@/i18n';
import { ApiRequestError } from '@/services/request';
import styles from './index.module.less';
import SourceSession from './SourceSession';

function ListColumn<T>({
  id,
  title,
  state,
  count,
  filtered,
  onRefresh,
  children,
}: {
  id: string;
  title: string;
  state: SettlementListState<T>;
  count: number;
  filtered: boolean;
  onRefresh: () => Promise<void>;
  children: ReactNode;
}) {
  const { t } = useAppI18n();
  return (
    <section className={styles.column} aria-labelledby={id} aria-busy={state.loading}>
      <div className={styles.columnHeader}>
        <h2 id={id}>{title}</h2>
        <span className={styles.count}>{state.loaded ? count : '—'}</span>
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined />}
          loading={state.loading}
          aria-label={`${t('assets.refresh')} ${title}`}
          onClick={() => void onRefresh()}
        />
      </div>
      {state.error ? (
        <Alert
          type="error"
          showIcon
          className={styles.listAlert}
          message={t('settlementCenter.loadError')}
          description={state.error.message}
          action={
            <Button size="small" onClick={() => void onRefresh()}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : null}
      {!state.loaded && state.loading ? (
        <div className={styles.loading}>
          <Spin />
          <span>{t('assets.loading')}</span>
        </div>
      ) : count ? (
        <div className={styles.list}>{children}</div>
      ) : state.loaded && !state.error ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t(filtered ? 'settlementCenter.noMatch' : 'settlementCenter.empty')}
        />
      ) : null}
    </section>
  );
}

export default function SettlementCenterContent({
  controller,
  sessions,
}: {
  controller: SettlementCenterController;
  sessions: ConsoleSession[];
}) {
  const { t, language } = useAppI18n();
  const sessionTitles = useMemo(
    () =>
      new Map(
        sessions.map((session) => [
          session.id,
          session.title?.trim() || t('common.unnamedSession'),
        ]),
      ),
    [sessions, t],
  );
  const [searchText, setSearchText] = useState('');
  const [view, setView] = useState<SettlementView>('pending');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({});
  const [review, setReview] = useState<SkillProposal | null>(null);
  const [reviewLoading, setReviewLoading] = useState<string | null>(null);
  const [selectedHunks, setSelectedHunks] = useState<Set<string>>(() => new Set());
  const lifetime = useRef(0);
  const reviewGeneration = useRef(0);
  useEffect(
    () => () => {
      lifetime.current += 1;
      reviewGeneration.current += 1;
    },
    [],
  );

  const groups = useMemo(
    () => ({
      pending: selectSettlementItems(
        controller.cards.items,
        controller.proposals.items,
        'pending',
        searchText,
      ),
      processed: selectSettlementItems(
        controller.cards.items,
        controller.proposals.items,
        'processed',
        searchText,
      ),
    }),
    [controller.cards.items, controller.proposals.items, searchText],
  );
  const visible = groups[view];
  const countKnown = controller.cards.loaded && controller.proposals.loaded;
  const viewLabel = (value: SettlementView) =>
    `${t(value === 'pending' ? 'settlementCenter.pending' : 'settlementCenter.processed')}${countKnown ? ` (${groups[value].cards.length + groups[value].proposals.length})` : ''}`;
  const timeLabel = (item: SettlementCard | SkillProposal) => {
    const value = settlementTimestamp(item, view);
    return value === null
      ? '—'
      : new Date(value).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');
  };
  const errorText = (error: unknown) => {
    if (error instanceof ApiRequestError && error.status === 404)
      return t('settlementCenter.sessionUnavailable');
    if (
      error instanceof ApiRequestError &&
      error.status === 409 &&
      error.details?.reason === 'SKILL_BUSY'
    )
      return t('skillProposal.busy');
    return error instanceof Error ? error.message : String(error);
  };
  const closeReview = () => {
    reviewGeneration.current += 1;
    setReview(null);
    setReviewLoading(null);
    setSelectedHunks(new Set());
  };
  const toggle = (key: string, initial: boolean) =>
    setExpanded((current) => ({ ...current, [key]: !(current[key] ?? initial) }));

  const handleCard = async (card: SettlementCard, action: 'confirm' | 'dismiss') => {
    const key = settlementItemKey(card);
    const generation = lifetime.current;
    const fields = normalizeFields({ ...card.fields, ...drafts[key] });
    if (action === 'confirm') {
      const missing = getMissingFields(card, fields);
      if (missing.length) {
        message.warning(
          t('assets.missingFields', {
            fields: missing
              .map((field) => (FIELD_LABEL_KEYS[field] ? t(FIELD_LABEL_KEYS[field]) : field))
              .join('、'),
          }),
        );
        return;
      }
    }
    try {
      const result = await (action === 'confirm'
        ? controller.confirmCard(card, fields)
        : controller.dismissCard(card));
      if (!result || generation !== lifetime.current) return;
      setDrafts((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      setExpanded((current) => ({ ...current, [key]: false }));
      message.success(
        t(action === 'confirm' ? 'assets.confirmedSuccess' : 'assets.dismissedSuccess'),
      );
    } catch (error) {
      if (generation === lifetime.current) message.error(errorText(error));
    }
  };

  const openReview = async (proposal: SkillProposal) => {
    if (!proposal.session_id?.trim()) {
      message.error(t('settlementCenter.missingSession'));
      return;
    }
    const generation = ++reviewGeneration.current;
    setReviewLoading(settlementItemKey(proposal));
    try {
      const response = await skillProposalApi.get(proposal.session_id, proposal.id);
      if (generation !== reviewGeneration.current) return;
      if (response.skill_proposal.status !== 'pending') {
        void controller.refreshProposals();
        message.info(t('settlementCenter.alreadyProcessed'));
        return;
      }
      setReview(response.skill_proposal);
      setSelectedHunks(
        new Set(collectSkillHunkIds(getSkillProposalFileChanges(response.skill_proposal))),
      );
    } catch (error) {
      if (generation === reviewGeneration.current) message.error(errorText(error));
    } finally {
      if (generation === reviewGeneration.current) setReviewLoading(null);
    }
  };

  const handleProposal = async (
    proposal: SkillProposal,
    action: 'accept' | 'reject',
    selection?: SkillProposalAcceptSelection,
  ) => {
    const generation = lifetime.current;
    try {
      const result = await (action === 'accept'
        ? controller.acceptProposal(proposal, selection)
        : controller.rejectProposal(proposal));
      if (!result || generation !== lifetime.current) return;
      setExpanded((current) => ({ ...current, [`skill:${settlementItemKey(proposal)}`]: false }));
      closeReview();
      message.success(t(action === 'accept' ? 'skillProposal.accepted' : 'skillProposal.rejected'));
    } catch (error) {
      if (generation === lifetime.current) message.error(errorText(error));
    }
  };
  const submitting = Boolean(review && controller.busy[`proposals:${settlementItemKey(review)}`]);

  return (
    <>
      <div className={styles.toolbar}>
        <Segmented
          aria-label={t('settlementCenter.statusView')}
          value={view}
          options={['pending', 'processed'].map((value) => ({
            value,
            label: viewLabel(value as SettlementView),
          }))}
          onChange={(value) => setView(value as SettlementView)}
        />
        <div className={styles.controls}>
          <Input
            className={styles.search}
            prefix={<SearchOutlined aria-hidden="true" />}
            placeholder={t('settlementCenter.search')}
            aria-label={t('settlementCenter.search')}
            allowClear
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
        </div>
      </div>
      {!controller.scope ? (
        <Alert
          className={styles.listAlert}
          type="warning"
          showIcon
          message={t('settlementCenter.identityUnavailable')}
        />
      ) : (
        <div className={styles.columns}>
          <ListColumn
            id="settlement-semantic-title"
            title={t('settlementCenter.semanticTitle')}
            state={controller.cards}
            count={visible.cards.length}
            filtered={Boolean(searchText.trim())}
            onRefresh={controller.refreshCards}
          >
            {visible.cards.map((card) => {
              const key = settlementItemKey(card);
              const pending = isPendingCard(card);
              return (
                <div key={key}>
                  <SettlementCardView
                    card={card}
                    alwaysShowTitle
                    timeLabel={timeLabel(card)}
                    footerStart={
                      <SourceSession
                        sessionId={card.session_id ?? ''}
                        title={sessionTitles.get(card.session_id)}
                      />
                    }
                    draftFields={pending ? { ...card.fields, ...drafts[key] } : undefined}
                    expanded={expanded[key] ?? pending}
                    onToggle={() => toggle(key, pending)}
                    busy={controller.busy[`cards:${key}`]}
                    actionsDisabled={!card.session_id?.trim()}
                    onFieldChange={(field, value) =>
                      setDrafts((current) => ({
                        ...current,
                        [key]: { ...current[key], [field]: value },
                      }))
                    }
                    onConfirm={() => void handleCard(card, 'confirm')}
                    onDismiss={() => void handleCard(card, 'dismiss')}
                  />
                  {!card.session_id?.trim() ? (
                    <p role="alert">{t('settlementCenter.missingSession')}</p>
                  ) : null}
                </div>
              );
            })}
          </ListColumn>
          <ListColumn
            id="settlement-skill-title"
            title={t('settlementCenter.skillTitle')}
            state={controller.proposals}
            count={visible.proposals.length}
            filtered={Boolean(searchText.trim())}
            onRefresh={controller.refreshProposals}
          >
            {visible.proposals.map((proposal) => {
              const key = settlementItemKey(proposal);
              const pending = proposal.status === 'pending';
              const busy = Boolean(controller.busy[`proposals:${key}`]);
              return (
                <SkillProposalCard
                  key={key}
                  proposal={proposal}
                  timeLabel={timeLabel(proposal)}
                  expanded={expanded[`skill:${key}`] ?? pending}
                  onToggle={() => toggle(`skill:${key}`, pending)}
                >
                  <footer className={styles.cardFooter}>
                    <SourceSession
                      sessionId={proposal.session_id ?? ''}
                      title={sessionTitles.get(proposal.session_id)}
                    />
                    {pending ? (
                      <div className={styles.actions}>
                        <Button
                          size="small"
                          disabled={busy || reviewLoading === key || !proposal.session_id?.trim()}
                          onClick={() => void handleProposal(proposal, 'reject')}
                        >
                          {t('skillProposal.reject')}
                        </Button>
                        <Button
                          type="primary"
                          size="small"
                          loading={reviewLoading === key}
                          disabled={busy || Boolean(reviewLoading) || !proposal.session_id?.trim()}
                          onClick={() => void openReview(proposal)}
                        >
                          {t('skillProposal.review')}
                        </Button>
                      </div>
                    ) : null}
                  </footer>
                  {!proposal.session_id?.trim() ? (
                    <p role="alert">{t('settlementCenter.missingSession')}</p>
                  ) : null}
                </SkillProposalCard>
              );
            })}
          </ListColumn>
        </div>
      )}
      <SkillProposalReviewModal
        review={review}
        selectedHunks={selectedHunks}
        submitting={submitting}
        onClose={() => {
          if (!submitting) closeReview();
        }}
        onReject={() => {
          if (review) void handleProposal(review, 'reject');
        }}
        onAcceptAll={() => {
          if (review) void handleProposal(review, 'accept');
        }}
        onAcceptSelected={() => {
          if (review && selectedHunks.size)
            void handleProposal(review, 'accept', { hunk_ids: [...selectedHunks] });
        }}
        toggleFile={(file, checked) =>
          setSelectedHunks((current) => {
            const next = new Set(current);
            collectSkillHunkIds([file]).forEach((id) => {
              if (checked) next.add(id);
              else next.delete(id);
            });
            return next;
          })
        }
        toggleHunk={(id, checked) =>
          setSelectedHunks((current) => {
            const next = new Set(current);
            if (checked) next.add(id);
            else next.delete(id);
            return next;
          })
        }
      />
    </>
  );
}
