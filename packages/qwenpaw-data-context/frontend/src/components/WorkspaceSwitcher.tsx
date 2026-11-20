import React, { useMemo, useState } from 'react';
import { Select, message } from 'antd';
import { switchWorkspace } from '@/auth';
import { useTranslation } from 'react-i18next';
import { useAuthStore, useActiveWorkspace, useWorkspaces, useAuthEnabled } from '@/store/authStore';

const WorkspaceSwitcher: React.FC = () => {
  const authEnabled = useAuthEnabled();
  const workspaces = useWorkspaces();
  const activeWs = useActiveWorkspace();
  const [switching, setSwitching] = useState(false);
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });

  const options = useMemo(
    () => workspaces.map((ws) => ({ label: ws.name, value: ws.id })),
    [workspaces],
  );

  if (!authEnabled || workspaces.length <= 1) return null;

  const handleChange = async (value: string) => {
    if (value === activeWs.id || switching) return;
    setSwitching(true);
    try {
      await switchWorkspace(value);
      await useAuthStore.getState().refresh();
      if (useAuthStore.getState().activeWorkspaceId !== value) {
        throw new Error(t('workspaceSwitcher.refreshMismatch'));
      }
      window.location.reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('workspaceSwitcher.switchFailed'));
      await useAuthStore
        .getState()
        .refresh()
        .catch(() => undefined);
      setSwitching(false);
    }
  };

  return (
    <Select
      value={activeWs.id}
      onChange={handleChange}
      options={options}
      style={{ minWidth: 140 }}
      size="small"
      variant="borderless"
      loading={switching}
      disabled={switching}
    />
  );
};

export default WorkspaceSwitcher;
