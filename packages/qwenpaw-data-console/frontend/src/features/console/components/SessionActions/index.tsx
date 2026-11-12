import { DeleteOutlined, EditOutlined } from '@ant-design/icons';
import { Input, Modal, Tooltip } from '@agentscope-ai/design';
import { useState } from 'react';
import type { ConsoleSession } from '../../api/types';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

interface SessionActionsProps {
  session: ConsoleSession;
  tone?: 'dark' | 'light';
  className?: string;
  onRename: (sessionId: string, title: string) => Promise<unknown>;
  onDelete: (sessionId: string) => Promise<unknown>;
}

type DialogMode = 'rename' | 'delete' | null;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function SessionActions({
  session,
  tone = 'light',
  className = '',
  onRename,
  onDelete,
}: SessionActionsProps) {
  const [dialogMode, setDialogMode] = useState<DialogMode>(null);
  const [draftTitle, setDraftTitle] = useState(session.title);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { t } = useAppI18n();
  const sessionTitle = session.title || t('common.unnamedSession');

  const closeDialog = () => {
    if (submitting) return;
    setDialogMode(null);
    setError(null);
  };

  const openRename = () => {
    setDraftTitle(session.title);
    setError(null);
    setDialogMode('rename');
  };

  const openDelete = () => {
    setError(null);
    setDialogMode('delete');
  };

  const handleRename = async () => {
    const nextTitle = draftTitle.trim();
    if (!nextTitle) {
      setError(t('sessions.nameRequired'));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onRename(session.id, nextTitle);
      setDialogMode(null);
    } catch (renameError) {
      setError(errorMessage(renameError));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await onDelete(session.id);
      setDialogMode(null);
    } catch (deleteError) {
      setError(errorMessage(deleteError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <span
        className={`${styles.actions} ${styles[tone]} ${className}`}
        onClick={(event) => event.stopPropagation()}
      >
        <Tooltip title={t('sessions.rename')}>
          <button
            className={styles.actionButton}
            type="button"
            aria-label={`${t('sessions.rename')}：${sessionTitle}`}
            onClick={openRename}
          >
            <EditOutlined />
          </button>
        </Tooltip>
        <Tooltip title={t('sessions.delete')}>
          <button
            className={`${styles.actionButton} ${styles.deleteButton}`}
            type="button"
            aria-label={`${t('sessions.delete')}：${sessionTitle}`}
            onClick={openDelete}
          >
            <DeleteOutlined />
          </button>
        </Tooltip>
      </span>

      {dialogMode === 'rename' ? (
        <Modal
          title={t('sessions.rename')}
          open={dialogMode === 'rename'}
          okText={t('common.save')}
          cancelText={t('common.cancel')}
          confirmLoading={submitting}
          okButtonProps={{ disabled: !draftTitle.trim() }}
          onOk={() => void handleRename()}
          onCancel={closeDialog}
        >
          <label className={styles.fieldLabel} htmlFor={`session-title-${session.id}`}>
            {t('sessions.name')}
          </label>
          <Input
            id={`session-title-${session.id}`}
            autoFocus
            maxLength={80}
            value={draftTitle}
            placeholder={t('sessions.namePlaceholder')}
            onChange={(event) => setDraftTitle(event.target.value)}
            onPressEnter={() => {
              if (draftTitle.trim() && !submitting) void handleRename();
            }}
          />
          {error ? <div className={styles.error}>{error}</div> : null}
        </Modal>
      ) : null}

      {dialogMode === 'delete' ? (
        <Modal
          title={t('sessions.delete')}
          open={dialogMode === 'delete'}
          okText={t('common.delete')}
          cancelText={t('common.cancel')}
          okButtonProps={{ danger: true }}
          confirmLoading={submitting}
          onOk={() => void handleDelete()}
          onCancel={closeDialog}
        >
          <p className={styles.deleteCopy}>{t('sessions.deleteConfirm', { name: sessionTitle })}</p>
          {session.status === 'running' ? (
            <p className={styles.warning}>{t('sessions.deleteRunningWarning')}</p>
          ) : null}
          {error ? <div className={styles.error}>{error}</div> : null}
        </Modal>
      ) : null}
    </>
  );
}
