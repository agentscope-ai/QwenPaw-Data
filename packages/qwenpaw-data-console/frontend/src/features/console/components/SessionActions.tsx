import {
  DeleteOutlined,
  EditOutlined,
} from '@ant-design/icons';
import { Input, Modal, Tooltip } from '@agentscope-ai/design';
import { useState } from 'react';
import type { ConsoleSession } from '../api/types';
import styles from './SessionActions.module.less';

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
      setError('会话名称不能为空');
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
        <Tooltip title="修改会话名称">
          <button
            className={styles.actionButton}
            type="button"
            aria-label={`修改会话名称：${session.title || '未命名会话'}`}
            onClick={openRename}
          >
            <EditOutlined />
          </button>
        </Tooltip>
        <Tooltip title="删除会话">
          <button
            className={`${styles.actionButton} ${styles.deleteButton}`}
            type="button"
            aria-label={`删除会话：${session.title || '未命名会话'}`}
            onClick={openDelete}
          >
            <DeleteOutlined />
          </button>
        </Tooltip>
      </span>

      <Modal
        title="修改会话名称"
        open={dialogMode === 'rename'}
        okText="保存"
        cancelText="取消"
        confirmLoading={submitting}
        okButtonProps={{ disabled: !draftTitle.trim() }}
        onOk={() => void handleRename()}
        onCancel={closeDialog}
      >
        <label className={styles.fieldLabel} htmlFor={`session-title-${session.id}`}>
          会话名称
        </label>
        <Input
          id={`session-title-${session.id}`}
          autoFocus
          maxLength={80}
          value={draftTitle}
          placeholder="请输入会话名称"
          onChange={(event) => setDraftTitle(event.target.value)}
          onPressEnter={() => {
            if (draftTitle.trim() && !submitting) void handleRename();
          }}
        />
        {error ? <div className={styles.error}>{error}</div> : null}
      </Modal>

      <Modal
        title="删除会话"
        open={dialogMode === 'delete'}
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={submitting}
        onOk={() => void handleDelete()}
        onCancel={closeDialog}
      >
        <p className={styles.deleteCopy}>
          确定删除“{session.title || '未命名会话'}”吗？删除后将无法恢复。
        </p>
        {session.status === 'running' ? (
          <p className={styles.warning}>运行中的会话需要先停止当前任务才能删除。</p>
        ) : null}
        {error ? <div className={styles.error}>{error}</div> : null}
      </Modal>
    </>
  );
}
