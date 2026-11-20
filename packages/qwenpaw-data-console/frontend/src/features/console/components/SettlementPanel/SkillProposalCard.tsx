import { DownOutlined } from '@ant-design/icons';
import { Tag } from 'antd';
import type { ReactNode } from 'react';
import type { SkillProposal } from '../../api/skillProposals';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

interface Props {
  proposal: SkillProposal;
  expanded?: boolean;
  timeLabel?: string;
  onToggle?: () => void;
  children?: ReactNode;
}

export default function SkillProposalCard({
  proposal,
  expanded = true,
  timeLabel,
  onToggle,
  children,
}: Props) {
  const { t } = useAppI18n();
  const title = (
    <span className={styles.settlementCardToggleTop}>
      <span className={styles.settlementCardTitleRow}>
        <Tag bordered={false} color="purple">
          Skill
        </Tag>
        <strong title={proposal.summary}>{proposal.summary}</strong>
      </span>
      {onToggle ? (
        <DownOutlined className={expanded ? styles.settlementCardExpandIconOpen : undefined} />
      ) : null}
    </span>
  );
  return (
    <article
      className={`${styles.settlementCard} ${proposal.status === 'pending' ? '' : styles.settlementCardReadOnly}`}
    >
      <header className={styles.settlementCardHeader}>
        {onToggle ? (
          <button
            type="button"
            className={styles.settlementCardToggle}
            aria-expanded={expanded}
            onClick={onToggle}
          >
            {title}
          </button>
        ) : (
          title
        )}
        {timeLabel ? <span className={styles.settlementCardMeta}>{timeLabel}</span> : null}
      </header>
      {expanded ? (
        <>
          <div className={styles.skillProposalMeta}>
            <span>
              {proposal.mentioned_skill_names.join(' · ') || t('skillProposal.unknownSkill')}
            </span>
            <span>{t('skillProposal.fileCount', { count: proposal.modified_file_count })}</span>
            {proposal.status !== 'pending' ? (
              <span>
                {proposal.status === 'accepted'
                  ? t('skillProposal.accepted')
                  : proposal.status === 'partial'
                    ? t('settlementCenter.partial')
                    : proposal.status === 'rejected'
                      ? t('skillProposal.rejected')
                      : proposal.status}
              </span>
            ) : null}
          </div>
          {children}
        </>
      ) : null}
    </article>
  );
}
