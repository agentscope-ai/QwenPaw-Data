/** Shell-level viewpoint switcher — shared by every Graph Center tab.
 *  Mirrors the header WorkspaceSwitcher pattern (commit 5463efd): scope
 *  controls live in the shell, not inside individual pages.
 *  - member: single static chip (server-enforced read_own_graph).
 *  - admin (ws_admin / tenant_admin): 全局 (the active workspace's global
 *    graph only) / 我的图 (own userspace) / one chip per workspace user from
 *    the auth member universe; clicking re-scopes the whole Graph Center
 *    dataset via setMemberId (server-enforced review_others_graph). */
import { useId, useLayoutEffect, useRef, useState } from 'react';
import { DownOutlined, UpOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useGraphCenter } from '../GraphCenterContext';
import styles from '../index.module.less';

export default function ViewerSwitcher() {
  const { t } = useTranslation();
  const { scope, memberId, setMemberId, memberOptions } = useGraphCenter();
  // Auth disabled → no session, treat as the global (admin) viewpoint.
  const isAdminView = scope.isAdmin || !scope.authEnabled;

  const [expanded, setExpanded] = useState(false);
  const [hasOverflow, setHasOverflow] = useState(false);
  const [rowHeight, setRowHeight] = useState<number>();
  const barRef = useRef<HTMLDivElement>(null);
  const labelRef = useRef<HTMLSpanElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  useLayoutEffect(() => {
    const bar = barRef.current;
    const label = labelRef.current;
    const list = listRef.current;
    if (!bar || !label || !list) return;
    const measure = () => {
      const chips = Array.from(list.children) as HTMLElement[];
      const gap = parseFloat(getComputedStyle(list).columnGap) || 0;
      const totalWidth =
        chips.reduce((sum, chip) => sum + chip.getBoundingClientRect().width, 0) +
        Math.max(0, chips.length - 1) * gap;
      // Measure against the space without the toggle, avoiding a resize loop
      // when showing the toggle itself pushes a chip onto the next row.
      const availableWidth =
        bar.clientWidth -
        label.getBoundingClientRect().width -
        parseFloat(getComputedStyle(label).marginRight) -
        gap;
      setHasOverflow(totalWidth > availableWidth + 1);
      setRowHeight(chips[0]?.getBoundingClientRect().height);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(bar);
    observer.observe(list);
    for (const chip of list.children) observer.observe(chip);
    return () => observer.disconnect();
  }, [isAdminView, memberOptions, scope.userId, scope.username, t]);

  return (
    <div ref={barRef} className={styles.viewerBar}>
      <span ref={labelRef} className={styles.chipsLabel}>
        {t('graphCenter.viewerLabel')}
      </span>
      <div
        id={listId}
        ref={listRef}
        className={styles.viewerChips}
        style={{ maxHeight: expanded ? undefined : rowHeight }}
      >
        {isAdminView ? (
          <>
            <span
              className={`${styles.chip} ${memberId === '' ? styles.chipActive : ''}`}
              title={t('graphCenter.viewerAdminTip')}
              onClick={() => setMemberId('')}
            >
              {t('graphCenter.viewerAdminChip')}
            </span>
            {scope.userId && (
              <span
                className={`${styles.chip} ${memberId === scope.userId ? styles.chipActive : ''}`}
                title={t('graphCenter.myGraphTip')}
                onClick={() => setMemberId(scope.userId)}
              >
                {t('graphCenter.myGraphChip')}
              </span>
            )}
            {memberOptions.map((m) => (
              <span
                key={m.id}
                className={`${styles.chip} ${memberId === m.id ? styles.chipActive : ''}`}
                onClick={() => setMemberId(m.id)}
              >
                {m.username || m.id.slice(0, 10)}
              </span>
            ))}
          </>
        ) : (
          <span className={`${styles.chip} ${styles.chipActive}`}>
            {scope.username || scope.userId}
          </span>
        )}
      </div>
      {hasOverflow && (
        <button
          type="button"
          className={styles.viewerToggle}
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
        >
          {t(expanded ? 'graphCenter.collapseViewers' : 'graphCenter.expandViewers')}
          {expanded ? <UpOutlined /> : <DownOutlined />}
        </button>
      )}
    </div>
  );
}
