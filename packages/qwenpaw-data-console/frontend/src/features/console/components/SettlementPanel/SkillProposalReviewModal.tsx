import { FileTextOutlined } from '@ant-design/icons';
import { Button } from '@agentscope-ai/design';
import { Checkbox, Empty, Modal } from 'antd';
import type { SkillFileChange, SkillProposal } from '../../api/skillProposals';
import {
  collectSkillHunkIds,
  countSelectedSkillHunks,
  getSkillProposalFileChanges,
} from '../../model/settlement/skillProposal';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

function renderDiffLines(diff: string) {
  return (diff || '').split('\n').map((line, index) => {
    const kind = line.startsWith('@@')
      ? 'meta'
      : line.startsWith('+') && !line.startsWith('+++')
        ? 'add'
        : line.startsWith('-') && !line.startsWith('---')
          ? 'del'
          : 'ctx';
    return (
      <span
        className={`${styles.skillDiffLine} ${styles[`skillDiffLine_${kind}`]}`}
        key={`${index}:${line}`}
      >
        <span className={styles.skillDiffLineContent}>{line || ' '}</span>
      </span>
    );
  });
}

interface Props {
  review: SkillProposal | null;
  selectedHunks: ReadonlySet<string>;
  submitting: boolean;
  onClose: () => void;
  onReject: () => void;
  onAcceptSelected: () => void;
  onAcceptAll: () => void;
  toggleFile: (file: SkillFileChange, checked: boolean) => void;
  toggleHunk: (hunkId: string, checked: boolean) => void;
}

export default function SkillProposalReviewModal({
  review,
  selectedHunks,
  submitting,
  onClose,
  onReject,
  onAcceptSelected,
  onAcceptAll,
  toggleFile,
  toggleHunk,
}: Props) {
  const { t } = useAppI18n();
  const files = review ? getSkillProposalFileChanges(review) : [];
  const allHunkIds = collectSkillHunkIds(files);
  const readOnly = review?.status !== 'pending';
  const formatHunkHeader = (header: string): string => {
    const match = /^edit\s+(\d+)\s*·\s*(.+)$/i.exec(header.trim());
    if (!match) return header;
    const [, index, location] = match;
    if (/^new file$/i.test(location)) {
      return t('skillProposal.hunkNewFile', { index });
    }
    const lineMatch = /^L(\d+)$/i.exec(location);
    if (lineMatch) {
      return t('skillProposal.hunkLine', { index, line: lineMatch[1] });
    }
    return header;
  };

  return (
    <Modal
      open={Boolean(review)}
      title={t('skillProposal.diffTitle')}
      width={780}
      rootClassName={styles.skillDiffModal}
      closable={!submitting}
      maskClosable={!submitting}
      onCancel={onClose}
      footer={
        <div className={styles.settlementCardActions}>
          <Button danger disabled={submitting || !review || readOnly} onClick={onReject}>
            {t('skillProposal.reject')}
          </Button>
          <Button
            disabled={submitting || !review || readOnly || !selectedHunks.size}
            onClick={onAcceptSelected}
          >
            {t('skillProposal.acceptSelected')}
          </Button>
          <Button
            type="primary"
            loading={submitting}
            disabled={!review || readOnly}
            onClick={onAcceptAll}
          >
            {t('skillProposal.acceptAll')}
          </Button>
        </div>
      }
    >
      {review ? (
        <div className={styles.skillDiffBody}>
          <p className={styles.skillDiffSummary}>{review.summary}</p>
          {files.length ? (
            files.map((file) => {
              const hunks = file.hunks ?? [];
              const selectedCount = countSelectedSkillHunks(file, selectedHunks);
              return (
                <section className={styles.skillDiffFile} key={file.patch_id}>
                  <div className={styles.skillDiffFileHead}>
                    <Checkbox
                      aria-label={`${file.skill_name}/${file.relative_path}`}
                      checked={hunks.length > 0 && selectedCount === hunks.length}
                      indeterminate={selectedCount > 0 && selectedCount < hunks.length}
                      disabled={submitting || readOnly || !hunks.length}
                      onChange={(event) => toggleFile(file, event.target.checked)}
                    />
                    <FileTextOutlined className={styles.skillDiffFileIcon} />
                    <code title={`${file.skill_name}/${file.relative_path}`}>
                      {file.skill_name}/{file.relative_path}
                    </code>
                    <span className={styles.skillDiffFileCount}>
                      {t('skillProposal.editCount', {
                        on: selectedCount,
                        total: hunks.length,
                      })}
                    </span>
                  </div>
                  <div className={styles.skillDiffChildren}>
                    {hunks.length ? (
                      hunks.map((hunk) => (
                        <div className={styles.skillDiffHunk} key={hunk.hunk_id}>
                          <div className={styles.skillDiffHunkBar}>
                            <Checkbox
                              aria-label={formatHunkHeader(hunk.header)}
                              checked={selectedHunks.has(hunk.hunk_id)}
                              disabled={submitting || readOnly}
                              onChange={(event) => toggleHunk(hunk.hunk_id, event.target.checked)}
                            />
                            <span title={formatHunkHeader(hunk.header)}>
                              {formatHunkHeader(hunk.header)}
                            </span>
                          </div>
                          {hunk.reason ? (
                            <p className={styles.skillDiffReason}>{hunk.reason}</p>
                          ) : null}
                          <div className={styles.skillDiffCodeFrame}>
                            <pre className={styles.skillDiffPre}>
                              <code className={styles.skillDiffCode}>
                                {renderDiffLines(hunk.diff || '')}
                              </code>
                            </pre>
                          </div>
                        </div>
                      ))
                    ) : (
                      <div className={styles.skillDiffHunk}>
                        <div className={styles.skillDiffCodeFrame}>
                          <pre className={styles.skillDiffPre}>
                            <code className={styles.skillDiffCode}>
                              {renderDiffLines(file.diff || '')}
                            </code>
                          </pre>
                        </div>
                      </div>
                    )}
                  </div>
                </section>
              );
            })
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('skillProposal.noChanges')}
            />
          )}
          {allHunkIds.length ? null : (
            <p className={styles.skillDiffHint}>{t('skillProposal.noSelectableHunks')}</p>
          )}
        </div>
      ) : null}
    </Modal>
  );
}
