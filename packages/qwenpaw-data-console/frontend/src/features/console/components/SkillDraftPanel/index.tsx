import {
  CheckOutlined,
  CloseOutlined,
  FileMarkdownOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Input, Modal, Select, Tag, Tooltip } from '@agentscope-ai/design';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { Alert, Empty, Popconfirm, Segmented, Spin, message } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { skillDraftsApi, type SkillDraft, type SkillDraftStatus } from '../../api/skillDrafts';
import { isSkillDraftBusy } from '../../model/skillDrafts/useSkillDrafts';
import type { ConversationTurn } from '../../types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

type PreviewMode = 'rendered' | 'source';

function stripSkillFrontmatter(content: string): string {
  const lines = content.replace(/\r\n?/g, '\n').split('\n');
  if (lines[0]?.trim() !== '---') return content;
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  return end < 0
    ? content
    : lines
        .slice(end + 1)
        .join('\n')
        .trimStart();
}

function canDismiss(status: SkillDraftStatus): boolean {
  return status === 'ready' || status === 'failed';
}

function isMarkdownFile(path: string): boolean {
  return /\.md(?:own)?$/i.test(path);
}

function sortSkillDraftFilePaths(files: Record<string, string>): string[] {
  return Object.keys(files).sort((left, right) => {
    if (left === 'SKILL.md') return -1;
    if (right === 'SKILL.md') return 1;
    return left.localeCompare(right);
  });
}

export default function SkillDraftPanel({
  sessionId,
  drafts,
  loading,
  error,
  turns,
  onRefresh,
}: {
  sessionId?: string | null;
  drafts: SkillDraft[];
  loading: boolean;
  error: string | null;
  turns: ConversationTurn[];
  onRefresh: () => Promise<void>;
}) {
  const { t } = useAppI18n();
  const [openDraftId, setOpenDraftId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDraft | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewMode>('rendered');
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [reviseText, setReviseText] = useState('');
  const [action, setAction] = useState<'revise' | 'confirm' | 'dismiss' | null>(null);
  const detailRequestSequenceRef = useRef(0);

  const turnNumbersById = useMemo(
    () => new Map(turns.map((turn) => [turn.id, turn.number])),
    [turns],
  );
  const selectedSummary = drafts.find((draft) => draft.id === openDraftId);

  const closeDraft = useCallback(() => {
    detailRequestSequenceRef.current += 1;
    setOpenDraftId(null);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(false);
    setPreviewMode('rendered');
    setSelectedFilePath(null);
    setReviseText('');
    setAction(null);
  }, []);

  const loadDetail = useCallback(
    async (draftId: string) => {
      if (!sessionId) return;
      const requestSequence = ++detailRequestSequenceRef.current;
      setDetailLoading(true);
      setDetailError(null);
      try {
        const result = await skillDraftsApi.get(sessionId, draftId);
        if (requestSequence !== detailRequestSequenceRef.current) return;
        setDetail(result.skill_draft);
      } catch (loadError) {
        if (requestSequence !== detailRequestSequenceRef.current) return;
        setDetailError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        if (requestSequence === detailRequestSequenceRef.current) setDetailLoading(false);
      }
    },
    [sessionId],
  );

  useEffect(() => {
    closeDraft();
  }, [closeDraft, sessionId]);

  useEffect(() => {
    if (!openDraftId) return;
    if (!detail || detail.id !== openDraftId || selectedSummary?.updated_at !== detail.updated_at) {
      void loadDetail(openDraftId);
    }
  }, [detail, loadDetail, openDraftId, selectedSummary?.updated_at]);

  const statusCopy = (status: SkillDraftStatus) => {
    switch (status) {
      case 'generating':
        return { label: t('skillDraft.statusGenerating'), color: 'processing' };
      case 'ready':
        return { label: t('skillDraft.statusReady'), color: 'warning' };
      case 'revising':
        return { label: t('skillDraft.statusRevising'), color: 'processing' };
      case 'accepted':
        return { label: t('skillDraft.statusAccepted'), color: 'success' };
      case 'dismissed':
        return { label: t('skillDraft.statusDismissed'), color: 'default' };
      case 'failed':
        return { label: t('skillDraft.statusFailed'), color: 'error' };
    }
  };

  const openDraft = (draft: SkillDraft) => {
    setOpenDraftId(draft.id);
    setDetail(null);
    setDetailError(null);
    setPreviewMode('rendered');
    setSelectedFilePath(null);
    setReviseText('');
  };

  const submitRevise = async () => {
    if (!sessionId || !detail || !reviseText.trim()) return;
    setAction('revise');
    try {
      const result = await skillDraftsApi.revise(sessionId, detail.id, reviseText.trim());
      setDetail(result.skill_draft);
      setReviseText('');
      await onRefresh();
    } catch (reviseError) {
      message.error(
        t('common.operationFailed', {
          message: reviseError instanceof Error ? reviseError.message : String(reviseError),
        }),
      );
    } finally {
      setAction(null);
    }
  };

  const submitConfirm = async () => {
    if (!sessionId || !detail) return;
    setAction('confirm');
    try {
      const result = await skillDraftsApi.confirm(sessionId, detail.id);
      message.success(t('skillDraft.confirmed', { name: result.created }));
      closeDraft();
      await onRefresh();
    } catch (confirmError) {
      message.error(
        t('common.operationFailed', {
          message: confirmError instanceof Error ? confirmError.message : String(confirmError),
        }),
      );
    } finally {
      setAction(null);
    }
  };

  const submitDismiss = async () => {
    if (!sessionId || !detail || !canDismiss(detail.status)) return;
    setAction('dismiss');
    try {
      await skillDraftsApi.dismiss(sessionId, detail.id);
      message.success(t('skillDraft.dismissed'));
      closeDraft();
      await onRefresh();
    } catch (dismissError) {
      message.error(
        t('common.operationFailed', {
          message: dismissError instanceof Error ? dismissError.message : String(dismissError),
        }),
      );
    } finally {
      setAction(null);
    }
  };

  const files = detail?.files ? sortSkillDraftFilePaths(detail.files) : [];
  const activeFilePath =
    selectedFilePath && files.includes(selectedFilePath) ? selectedFilePath : (files[0] ?? null);
  const preview = activeFilePath ? (detail?.files?.[activeFilePath] ?? '') : '';
  const markdownPreview = activeFilePath === 'SKILL.md' ? stripSkillFrontmatter(preview) : preview;
  const activeFileIsMarkdown = activeFilePath ? isMarkdownFile(activeFilePath) : false;
  const activePreviewMode = activeFileIsMarkdown ? previewMode : 'source';
  const ready = detail?.status === 'ready';
  const waiting = detail ? isSkillDraftBusy(detail.status) : false;

  return (
    <section className={`${styles.workspaceContentSection} ${styles.skillDraftPanel}`}>
      <div className={styles.workspaceSectionHeader}>
        <div>
          <strong>{t('skillDraft.title')}</strong>
          <span>
            {!sessionId
              ? t('assets.noSession')
              : loading && !drafts.length
                ? t('skillDraft.loading')
                : t('skillDraft.count', { count: drafts.length })}
          </span>
        </div>
        <Tooltip title={t('skillDraft.refresh')}>
          <Button
            type="text"
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            disabled={!sessionId}
            aria-label={t('skillDraft.refresh')}
            onClick={() => void onRefresh()}
          />
        </Tooltip>
      </div>

      {error ? (
        <div className={styles.skillDraftError} role="alert">
          <span>{error}</span>
          <Button type="link" size="small" onClick={() => void onRefresh()}>
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      {drafts.length ? (
        <div className={styles.skillDraftList}>
          {drafts.map((draft) => {
            const status = statusCopy(draft.status);
            const turnNumber = turnNumbersById.get(draft.source_chat_id);
            return (
              <button
                className={styles.skillDraftCard}
                type="button"
                key={draft.id}
                onClick={() => openDraft(draft)}
              >
                <span className={styles.skillDraftCardTitle}>
                  <FileMarkdownOutlined aria-hidden="true" />
                  <strong>{draft.name || t('skillDraft.untitled')}</strong>
                  <Tag bordered={false} color={status.color} size="small">
                    {status.label}
                  </Tag>
                </span>
                <span className={styles.skillDraftCardMeta}>
                  {turnNumber
                    ? t('assets.fromChat', { number: turnNumber })
                    : t('assets.fromCurrentSession')}
                </span>
              </button>
            );
          })}
        </div>
      ) : !error ? (
        <Empty
          className={styles.skillDraftEmpty}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('skillDraft.empty')}
        />
      ) : null}

      <Modal
        className={styles.skillDraftModal}
        width={900}
        open={Boolean(openDraftId)}
        title={detail?.name || selectedSummary?.name || t('skillDraft.untitled')}
        footer={null}
        destroyOnHidden
        onCancel={closeDraft}
      >
        {detailLoading && !detail ? (
          <div className={styles.skillDraftLoading}>
            <Spin />
            <span>{t('common.loading')}</span>
          </div>
        ) : null}
        {detailError ? (
          <Alert
            showIcon
            type="error"
            message={t('skillDraft.loadFailed')}
            description={detailError}
            action={
              openDraftId ? (
                <Button size="small" onClick={() => void loadDetail(openDraftId)}>
                  {t('common.retry')}
                </Button>
              ) : null
            }
          />
        ) : null}
        {detail ? (
          <div className={styles.skillDraftDetail}>
            <div className={styles.skillDraftDetailMeta}>
              <Tag bordered={false} color={statusCopy(detail.status).color}>
                {statusCopy(detail.status).label}
              </Tag>
              <span>{t('skillDraft.fileCount', { count: files.length })}</span>
              {detailLoading ? <Spin size="small" /> : null}
            </div>

            {waiting ? <Alert showIcon type="info" message={t('skillDraft.waitingHint')} /> : null}
            {detail.status === 'failed' ? (
              <Alert showIcon type="error" message={t('skillDraft.failedHint')} />
            ) : null}

            <div className={styles.skillDraftFileViewer}>
              {activeFilePath ? (
                <div className={styles.skillDraftFileToolbar}>
                  <Select
                    className={styles.skillDraftFileSelect}
                    value={activeFilePath}
                    options={files.map((filePath) => ({ value: filePath, label: filePath }))}
                    aria-label={t('skillDraft.files')}
                    showSearch
                    optionFilterProp="label"
                    onChange={setSelectedFilePath}
                  />
                  {activeFileIsMarkdown ? (
                    <Segmented<PreviewMode>
                      size="small"
                      value={previewMode}
                      options={[
                        { value: 'rendered', label: t('skillDraft.rendered') },
                        { value: 'source', label: t('skillDraft.source') },
                      ]}
                      onChange={setPreviewMode}
                    />
                  ) : (
                    <Tag bordered={false}>{t('skillDraft.source')}</Tag>
                  )}
                </div>
              ) : null}
              <div className={styles.skillDraftPreview}>
                {activeFilePath ? (
                  activePreviewMode === 'rendered' ? (
                    <Markdown
                      className={styles.skillDraftMarkdown}
                      content={markdownPreview}
                      baseFontSize={13}
                      baseLineHeight={1.7}
                    />
                  ) : (
                    <pre className={styles.skillDraftSource}>{preview}</pre>
                  )
                ) : (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={t('skillDraft.noFile')}
                  />
                )}
              </div>
            </div>

            {ready ? (
              <label className={styles.skillDraftRevise}>
                <span>{t('skillDraft.revise')}</span>
                <Input.TextArea
                  rows={3}
                  value={reviseText}
                  disabled={Boolean(action)}
                  placeholder={t('skillDraft.revisePlaceholder')}
                  onChange={(event) => setReviseText(event.target.value)}
                />
              </label>
            ) : null}

            <footer className={styles.skillDraftActions}>
              <Button onClick={closeDraft}>{t('common.cancel')}</Button>
              {canDismiss(detail.status) ? (
                <Button
                  danger
                  icon={<CloseOutlined />}
                  loading={action === 'dismiss'}
                  disabled={Boolean(action) && action !== 'dismiss'}
                  onClick={() => void submitDismiss()}
                >
                  {t('skillDraft.dismiss')}
                </Button>
              ) : null}
              {ready ? (
                <>
                  <Button
                    loading={action === 'revise'}
                    disabled={Boolean(action) || !reviseText.trim()}
                    onClick={() => void submitRevise()}
                  >
                    {t('skillDraft.revise')}
                  </Button>
                  <Popconfirm
                    title={t('skillDraft.confirmTitle')}
                    description={t('skillDraft.confirmDescription')}
                    okText={t('skillDraft.confirm')}
                    cancelText={t('common.cancel')}
                    onConfirm={submitConfirm}
                  >
                    <Button
                      type="primary"
                      icon={<CheckOutlined />}
                      loading={action === 'confirm'}
                      disabled={Boolean(action)}
                    >
                      {t('skillDraft.confirm')}
                    </Button>
                  </Popconfirm>
                </>
              ) : null}
            </footer>
          </div>
        ) : null}
      </Modal>
    </section>
  );
}
