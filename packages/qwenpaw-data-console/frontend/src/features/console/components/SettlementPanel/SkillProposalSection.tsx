import { ReloadOutlined } from '@ant-design/icons';
import { Button } from '@agentscope-ai/design';
import { Empty, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type {
  SkillFileChange,
  SkillProposal,
  SkillProposalAcceptSelection,
} from '../../api/skillProposals';
import { skillProposalApi } from '../../api/skillProposals';
import {
  collectSkillHunkIds,
  getSkillProposalFileChanges,
} from '../../model/settlement/skillProposal';
import { ApiRequestError } from '@/services/request';
import { useAppI18n } from '@/i18n';
import type { MessageKey } from '@/i18n/locales';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import ownerStyles from './index.module.less';
import SkillProposalCard from './SkillProposalCard';
import SkillProposalReviewModal from './SkillProposalReviewModal';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface SkillProposalSectionProps {
  sessionId: string | null | undefined;
  pending: SkillProposal[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void | Promise<void>;
  onAccept: (
    proposalId: string,
    selection?: SkillProposalAcceptSelection,
  ) => Promise<SkillProposal | undefined>;
  onReject: (proposalId: string) => Promise<SkillProposal | undefined>;
}

function isApiReason(error: unknown, status: number, reason: string): boolean {
  return (
    error instanceof ApiRequestError && error.status === status && error.details?.reason === reason
  );
}

export default function SkillProposalSection({
  sessionId,
  pending,
  loading,
  error,
  onRefresh,
  onAccept,
  onReject,
}: SkillProposalSectionProps) {
  const { t } = useAppI18n();
  const [review, setReview] = useState<SkillProposal | null>(null);
  const [reviewLoadingId, setReviewLoadingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [selectedHunks, setSelectedHunks] = useState<Set<string>>(() => new Set());

  const reviewGeneration = useRef(0);

  useEffect(() => {
    reviewGeneration.current += 1;
    setReview(null);
    setReviewLoadingId(null);
    setSubmitting(false);
    setSelectedHunks(new Set());
    return () => {
      reviewGeneration.current += 1;
    };
  }, [sessionId]);

  useEffect(() => {
    if (review && !pending.some((proposal) => proposal.id === review.id)) {
      setReview(null);
      setSelectedHunks(new Set());
    }
  }, [pending, review]);

  const actionErrorMessage = (actionError: unknown): string => {
    if (isApiReason(actionError, 409, 'SKILL_BUSY')) {
      return t('skillProposal.busy');
    }
    if (actionError instanceof ApiRequestError && actionError.status === 404) {
      return t('skillProposal.replaced');
    }
    return actionError instanceof Error ? actionError.message : String(actionError);
  };

  const openReview = async (proposal: SkillProposal) => {
    if (!proposal.session_id || submitting || reviewLoadingId) return;
    const generation = ++reviewGeneration.current;
    setReviewLoadingId(proposal.id);
    try {
      const detail = await skillProposalApi.get(proposal.session_id, proposal.id);
      if (generation !== reviewGeneration.current) return;
      const nextProposal = detail.skill_proposal;
      setSelectedHunks(new Set(collectSkillHunkIds(getSkillProposalFileChanges(nextProposal))));
      setReview(nextProposal);
    } catch (detailError) {
      if (generation !== reviewGeneration.current) return;
      message.error(actionErrorMessage(detailError));
      if (detailError instanceof ApiRequestError && detailError.status === 404) {
        void onRefresh();
      }
    } finally {
      if (generation === reviewGeneration.current) setReviewLoadingId(null);
    }
  };

  const toggleFile = (file: SkillFileChange, checked: boolean) => {
    const hunkIds = collectSkillHunkIds([file]);
    setSelectedHunks((current) => {
      const next = new Set(current);
      hunkIds.forEach((hunkId) => (checked ? next.add(hunkId) : next.delete(hunkId)));
      return next;
    });
  };

  const toggleHunk = (hunkId: string, checked: boolean) => {
    setSelectedHunks((current) => {
      const next = new Set(current);
      if (checked) next.add(hunkId);
      else next.delete(hunkId);
      return next;
    });
  };

  const runAction = async (
    work: () => Promise<SkillProposal | undefined>,
    successKey: MessageKey,
  ) => {
    setSubmitting(true);
    try {
      const result = await work();
      if (!result) return;
      message.success(t(successKey));
      setReview(null);
      setSelectedHunks(new Set());
    } catch (actionError) {
      message.error(actionErrorMessage(actionError));
      if (actionError instanceof ApiRequestError && actionError.status === 404) {
        void onRefresh();
      }
    } finally {
      setSubmitting(false);
    }
  };

  const acceptSelected = () => {
    if (!review) return;
    if (!selectedHunks.size) {
      message.warning(t('skillProposal.selectHunk'));
      return;
    }
    void runAction(
      () => onAccept(review.id, { hunk_ids: [...selectedHunks] }),
      'skillProposal.accepted',
    );
  };

  return (
    <section className={`${styles.workspaceContentSection} ${styles.settlementPanel}`}>
      <div className={styles.workspaceSectionHeader}>
        <div>
          <strong>{t('skillProposal.title')}</strong>
          <span>
            {!sessionId
              ? t('assets.noSession')
              : loading && !pending.length
                ? t('assets.detecting')
                : pending.length
                  ? t('skillProposal.pendingCount', { count: pending.length })
                  : t('skillProposal.autoDetect')}
          </span>
        </div>
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          disabled={!sessionId}
          aria-label={t('skillProposal.refresh')}
          onClick={() => void onRefresh()}
        />
      </div>

      {error ? (
        <div className={styles.settlementError} role="alert">
          <span>{error}</span>
          <Button type="link" size="small" onClick={() => void onRefresh()}>
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      {pending.length ? (
        <div className={styles.settlementList}>
          {pending.map((proposal) => (
            <SkillProposalCard key={proposal.id} proposal={proposal}>
              <footer className={styles.settlementCardActions}>
                <Button
                  size="small"
                  disabled={submitting || Boolean(reviewLoadingId)}
                  onClick={() =>
                    void runAction(() => onReject(proposal.id), 'skillProposal.rejected')
                  }
                >
                  {t('skillProposal.reject')}
                </Button>
                <Button
                  type="primary"
                  size="small"
                  loading={reviewLoadingId === proposal.id}
                  disabled={submitting || Boolean(reviewLoadingId)}
                  onClick={() => void openReview(proposal)}
                >
                  {t('skillProposal.review')}
                </Button>
              </footer>
            </SkillProposalCard>
          ))}
        </div>
      ) : !error ? (
        <Empty
          className={styles.workspaceEmptyState}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('skillProposal.empty')}
        />
      ) : null}

      <SkillProposalReviewModal
        review={review}
        selectedHunks={selectedHunks}
        submitting={submitting}
        onClose={() => {
          if (!submitting) {
            reviewGeneration.current += 1;
            setReview(null);
          }
        }}
        onReject={() => {
          if (review) void runAction(() => onReject(review.id), 'skillProposal.rejected');
        }}
        onAcceptSelected={acceptSelected}
        onAcceptAll={() => {
          if (review) void runAction(() => onAccept(review.id), 'skillProposal.accepted');
        }}
        toggleFile={toggleFile}
        toggleHunk={toggleHunk}
      />
    </section>
  );
}
