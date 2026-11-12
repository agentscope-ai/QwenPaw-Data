import { Button, Modal, Select, Space, Tooltip, Typography, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { useAppI18n } from '@/i18n';
import { buildConsoleSessionPath } from '@/shared/navigation/sessionNavigation';
import type { ConsoleSession, SessionShareScope } from '../../api/types';

export default function SessionShareModal({
  session,
  onClose,
  onSave,
}: {
  session: ConsoleSession; // Loaded session with confirmed ownership.
  onClose: () => void; // Close when dismissed or ownership changes.
  onSave: (sessionId: string, scope: SessionShareScope) => Promise<ConsoleSession>; // Saves via guarded controller.
}) {
  const { t } = useAppI18n();
  const savedScope = session.share_scope ?? 'none';
  const [scope, setScope] = useState<SessionShareScope>(savedScope);
  const [saving, setSaving] = useState(false);
  const mounted = useRef(true);
  const savingRef = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const save = async () => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      await onSave(session.id, scope);
      if (mounted.current) message.success(t('sessionShare.saved'));
    } catch (error) {
      if (mounted.current)
        message.error(
          t('sessionShare.saveFailed', {
            message: error instanceof Error ? error.message : String(error),
          }),
        );
    } finally {
      savingRef.current = false;
      if (mounted.current) setSaving(false);
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(
        new URL(buildConsoleSessionPath(session.id), window.location.origin).href,
      );
      if (mounted.current) message.success(t('sessionShare.copied'));
    } catch {
      if (mounted.current) message.error(t('sessionShare.copyFailed'));
    }
  };

  return (
    <Modal
      open
      title={t('sessionShare.title')}
      onCancel={onClose}
      footer={
        <Space>
          <Button
            type="primary"
            loading={saving}
            disabled={scope === savedScope}
            onClick={() => void save()}
          >
            {t('common.save')}
          </Button>
          <Tooltip
            title={savedScope === 'none' ? t('sessionShare.closed') : undefined}
            trigger={['hover', 'focus']}
          >
            <span
              style={{ display: 'inline-flex' }}
              tabIndex={savedScope === 'none' ? 0 : undefined}
            >
              <Button disabled={savedScope === 'none' || saving} onClick={() => void copy()}>
                {t('sessionShare.copy')}
              </Button>
            </span>
          </Tooltip>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
          <label htmlFor="session-share-scope" style={{ flexShrink: 0 }}>
            {t('sessionShare.scope')}
          </label>
          <Select
            id="session-share-scope"
            value={scope}
            disabled={saving}
            onChange={setScope}
            style={{ flex: 1, minWidth: 0 }}
            options={(['none', 'workspace', 'tenant', 'anyone'] as const).map((value) => ({
              value,
              label: t(`sessionShare.${value}`),
            }))}
          />
        </div>
        <Typography.Paragraph type="secondary">
          {t('sessionShare.loginRequired')}
        </Typography.Paragraph>
      </Space>
    </Modal>
  );
}
