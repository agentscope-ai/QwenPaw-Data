// ============================================================
// MgSyncBar — userspace MG sync status bar (self view: every role —
// member, ws_admin, tenant_admin — has a personal userspace graph)
//
// - Sync status Tag + "pull global updates" button (digest watermark)
// - Conflict MCQ modal: all conflicts of one pull batched in a single
//   modal; each item is a Radio MCQ (keep global / keep mine) with a
//   field-level diff; bulk shortcuts; submit only when all decided.
//   409 (global changed) → prompt re-pull and close; closing without
//   submitting keeps needs_sync=true and re-pops on the next pull.
// - APPEND-only entries: create-node / create-edge floats (no edit/delete).
// ============================================================

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Dropdown } from 'antd';
import { Button, Modal, Radio, Space, Tag, Tooltip, message } from '@/design';
import { CloudSyncOutlined, PlusOutlined } from '@/design';
import { useAuthStore } from '@/store/authStore';
import {
  MgSyncHttpError,
  fetchMgSyncStatus,
  resolveMgSync,
  syncUserspaceMg,
  type MgConflict,
  type MgSyncStatus,
} from '@/services/cmgraph/mgSync';
import MgAppendFloat, { type MgAppendKind } from './MgAppendFloat';

interface MgSyncBarProps {
  /** Reload the mg-zone graph after a successful sync/append. */
  onGraphChanged: () => void;
  /** Inline (toolbar) layout: slim, no own row, append actions in a dropdown. */
  compact?: boolean;
  /** Host append UI elsewhere (e.g. Graph Center canvas overlay). */
  onOpenAppend?: (kind: MgAppendKind) => void;
}

type Decision = 'global' | 'user';

/** ISO → 本地短格式（08-15 14:33）；解析失败回退原串。 */
function fmtSyncTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function diffFields(conflict: MgConflict): string[] {
  const keys = new Set([
    ...Object.keys(conflict.global_props || {}),
    ...Object.keys(conflict.user_props || {}),
  ]);
  const changed: string[] = [];
  keys.forEach((k) => {
    if (JSON.stringify(conflict.global_props?.[k]) !== JSON.stringify(conflict.user_props?.[k])) {
      changed.push(k);
    }
  });
  return changed.sort();
}

function PropsDiff({ conflict }: { conflict: MgConflict }) {
  const { t } = useTranslation();
  const changed = useMemo(() => new Set(diffFields(conflict)), [conflict]);
  const keys = useMemo(
    () =>
      Array.from(
        new Set([
          ...Object.keys(conflict.global_props || {}),
          ...Object.keys(conflict.user_props || {}),
        ]),
      ).sort((a, b) => {
        const ca = changed.has(a) ? 0 : 1;
        const cb = changed.has(b) ? 0 : 1;
        return ca - cb || a.localeCompare(b);
      }),
    [conflict, changed],
  );
  const render = (value: unknown) =>
    value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);
  return (
    <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ textAlign: 'left', color: '#888' }}>
          <th style={{ width: '20%', padding: '2px 8px' }}>{t('kgBrowser.mgSync.field')}</th>
          <th style={{ width: '40%', padding: '2px 8px' }}>
            {t('kgBrowser.mgSync.globalVersion')}
          </th>
          <th style={{ width: '40%', padding: '2px 8px' }}>{t('kgBrowser.mgSync.myVersion')}</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => {
          const highlight = changed.has(k);
          const cellStyle = {
            padding: '2px 8px',
            verticalAlign: 'top' as const,
            background: highlight ? 'rgba(250, 173, 20, 0.12)' : undefined,
            fontWeight: highlight ? 600 : undefined,
            wordBreak: 'break-all' as const,
          };
          return (
            <tr key={k}>
              <td style={cellStyle}>{k}</td>
              <td style={cellStyle}>{render(conflict.global_props?.[k])}</td>
              <td style={cellStyle}>{render(conflict.user_props?.[k])}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function MgSyncBar({
  onGraphChanged,
  compact = false,
  onOpenAppend,
}: MgSyncBarProps) {
  const { t } = useTranslation();
  const canSync = useAuthStore(
    (s) => !s.authEnabled || s.capabilities.includes('mg_userspace:sync'),
  );
  const [status, setStatus] = useState<MgSyncStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);

  // Conflict MCQ modal state
  const [conflicts, setConflicts] = useState<MgConflict[]>([]);
  const [conflictDigest, setConflictDigest] = useState('');
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [resolving, setResolving] = useState(false);

  const [appendKind, setAppendKind] = useState<MgAppendKind | null>(null);

  const openAppend = useCallback(
    (kind: MgAppendKind) => {
      if (onOpenAppend) onOpenAppend(kind);
      else setAppendKind(kind);
    },
    [onOpenAppend],
  );

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true);
    try {
      const next = await fetchMgSyncStatus();
      setStatus(next);
      return next;
    } catch (err) {
      // Status failure does not revoke the session's sync capability.
      message.error((err as Error).message);
      setStatus(null);
      return null;
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    if (canSync) void refreshStatus();
  }, [canSync, refreshStatus]);

  useEffect(() => {
    if (!canSync) return;
    if (status?.space_status !== 'uninitialized' && status?.space_status !== 'initializing') return;
    let cancelled = false;
    const timer = setTimeout(() => {
      void refreshStatus().then((next) => {
        if (!cancelled && next?.space_status === 'ready') onGraphChanged();
      });
    }, 6000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [canSync, status, refreshStatus, onGraphChanged]);

  const handleSync = useCallback(async () => {
    setSyncing(true);
    try {
      const result = await syncUserspaceMg();
      const summary = t('kgBrowser.mgSync.resultSummary', {
        created: result.created,
        overwritten: result.overwritten,
        deleted: result.deleted,
        adopted: result.auto_adopted,
        conflicts: result.conflict_count,
      });
      if (result.conflict_count > 0) {
        message.warning(summary);
        setConflicts(result.conflicts);
        setConflictDigest(result.global_digest);
        setDecisions({});
      } else {
        message.success(summary);
      }
      await refreshStatus();
      onGraphChanged();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSyncing(false);
    }
  }, [t, refreshStatus, onGraphChanged]);

  const allDecided = conflicts.length > 0 && conflicts.every((c) => decisions[c.key] !== undefined);

  const handleResolve = useCallback(async () => {
    setResolving(true);
    try {
      const result = await resolveMgSync(
        conflictDigest,
        conflicts.map((c) => ({
          kind: 'node' as const,
          label: c.label,
          key: c.key,
          decision: decisions[c.key],
        })),
      );
      message.success(t('kgBrowser.mgSync.resolveDone', { resolved: result.resolved }));
      setConflicts([]);
      await refreshStatus();
      onGraphChanged();
    } catch (err) {
      if (err instanceof MgSyncHttpError && err.status === 409) {
        message.warning(t('kgBrowser.mgSync.globalChanged'));
        setConflicts([]);
        await refreshStatus();
      } else {
        message.error((err as Error).message);
      }
    } finally {
      setResolving(false);
    }
  }, [conflictDigest, conflicts, decisions, t, refreshStatus, onGraphChanged]);

  if (!canSync) return null;

  const statusTag = !status ? (
    <Tag color={statusLoading ? undefined : 'orange'}>
      {t(statusLoading ? 'kgBrowser.mgSync.loading' : 'kgBrowser.mgSync.statusUnavailable')}
    </Tag>
  ) : status.space_status === 'uninitialized' || status.space_status === 'initializing' ? (
    <Tag color="orange">{t(`kgBrowser.mgSync.${status.space_status}`)}</Tag>
  ) : !status.last_synced_at ? (
    <Tag color="default">{t('kgBrowser.mgSync.neverSynced')}</Tag>
  ) : status.needs_sync ? (
    <Tag color="orange">{t('kgBrowser.mgSync.needsSync')}</Tag>
  ) : (
    <Tag color="green">{t('kgBrowser.mgSync.synced')}</Tag>
  );

  const appendMenu = {
    items: [
      {
        key: 'node',
        icon: <PlusOutlined />,
        label: t('kgBrowser.mgSync.appendNode'),
        onClick: () => openAppend('node'),
      },
      {
        key: 'edge',
        icon: <PlusOutlined />,
        label: t('kgBrowser.mgSync.appendEdge'),
        onClick: () => openAppend('edge'),
      },
    ],
  };

  const lastSynced = status?.last_synced_at ? (
    <Tooltip title={status.last_synced_at}>
      <span style={{ fontSize: 11, color: '#8c8c8c' }}>
        {compact
          ? fmtSyncTime(status.last_synced_at)
          : t('kgBrowser.mgSync.lastSyncedAt', { time: fmtSyncTime(status.last_synced_at) })}
      </span>
    </Tooltip>
  ) : null;

  const pullLabel = t(
    !status && !statusLoading
      ? 'kgBrowser.mgSync.retry'
      : status && !status.last_synced_at
        ? 'kgBrowser.mgSync.initialize'
        : 'kgBrowser.mgSync.pull',
  );
  const pullBtn = (
    <Tooltip title={t('kgBrowser.mgSync.pullTooltip')}>
      <Button
        size="small"
        type="primary"
        icon={<CloudSyncOutlined />}
        loading={syncing}
        disabled={syncing}
        onClick={handleSync}
        aria-label={pullLabel}
      >
        {compact && status?.last_synced_at ? undefined : pullLabel}
      </Button>
    </Tooltip>
  );

  const modals = (
    <>
      {/* Conflict MCQ modal — one modal for the whole batch */}
      <Modal
        title={t('kgBrowser.mgSync.conflictTitle', { count: conflicts.length })}
        open={conflicts.length > 0}
        width={860}
        onCancel={() => {
          // No decision persisted; needs_sync stays true and the same
          // batch re-pops on the next pull (idempotent re-diff).
          setConflicts([]);
          message.info(t('kgBrowser.mgSync.conflictDeferred'));
        }}
        footer={[
          <Button
            key="all-global"
            onClick={() =>
              setDecisions(Object.fromEntries(conflicts.map((c) => [c.key, 'global' as Decision])))
            }
          >
            {t('kgBrowser.mgSync.allGlobal')}
          </Button>,
          <Button
            key="all-user"
            onClick={() =>
              setDecisions(Object.fromEntries(conflicts.map((c) => [c.key, 'user' as Decision])))
            }
          >
            {t('kgBrowser.mgSync.allUser')}
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={resolving}
            disabled={!allDecided}
            onClick={handleResolve}
          >
            {t('kgBrowser.mgSync.submitDecisions')}
          </Button>,
        ]}
      >
        <div style={{ maxHeight: 480, overflow: 'auto' }}>
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {conflicts.map((conflict) => (
              <div
                key={conflict.key}
                style={{
                  border: '1px solid rgba(0,0,0,0.08)',
                  borderRadius: 8,
                  padding: 12,
                }}
              >
                <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Tag color="blue">{conflict.label}</Tag>
                  <code style={{ fontSize: 12 }}>{conflict.key}</code>
                </div>
                <PropsDiff conflict={conflict} />
                <div style={{ marginTop: 8 }}>
                  <Radio.Group
                    value={decisions[conflict.key]}
                    onChange={(e) =>
                      setDecisions((prev) => ({ ...prev, [conflict.key]: e.target.value }))
                    }
                    optionType="button"
                    buttonStyle="solid"
                  >
                    <Radio.Button value="global">{t('kgBrowser.mgSync.keepGlobal')}</Radio.Button>
                    <Radio.Button value="user">{t('kgBrowser.mgSync.keepUser')}</Radio.Button>
                  </Radio.Group>
                </div>
              </div>
            ))}
          </Space>
        </div>
      </Modal>
      {!onOpenAppend && (
        <MgAppendFloat
          kind={appendKind}
          variant="page"
          onClose={() => setAppendKind(null)}
          onSuccess={onGraphChanged}
        />
      )}
    </>
  );

  // Compact: inline fragment meant to sit inside the Graph Center toolbar —
  // no own row, icon-only pull button, append actions in one dropdown.
  if (compact) {
    return (
      <>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          {statusTag}
          {lastSynced}
          {pullBtn}
          <Tooltip title={t('kgBrowser.mgSync.append')}>
            <Dropdown menu={appendMenu} trigger={['click']}>
              <Button size="small" icon={<PlusOutlined />} />
            </Dropdown>
          </Tooltip>
        </span>
        {modals}
      </>
    );
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 12px',
        borderBottom: '1px solid rgba(0,0,0,0.06)',
        flexWrap: 'wrap',
      }}
    >
      {statusTag}
      {lastSynced}
      {pullBtn}
      <span style={{ flex: 1 }} />
      <Button size="small" icon={<PlusOutlined />} onClick={() => openAppend('node')}>
        {t('kgBrowser.mgSync.appendNode')}
      </Button>
      <Button size="small" icon={<PlusOutlined />} onClick={() => openAppend('edge')}>
        {t('kgBrowser.mgSync.appendEdge')}
      </Button>
      {modals}
    </div>
  );
}
