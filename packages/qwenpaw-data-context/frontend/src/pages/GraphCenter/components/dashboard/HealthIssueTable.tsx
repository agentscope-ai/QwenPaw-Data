/** Compact health-issue list — the actionable remainder of the old Audit page.
 *  Filter chips + 8-row preview, styled like CuratedAssets / HotspotList.
 *  Row click → entity detail; locate stays a secondary action. */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import type { GraphDb } from '../../dataset/graphDb';
import { typeLabel } from '../../dataset/graphDb';
import type { HealthIssueCode } from '../../types';
import { CrosshairIcon } from '../LocateButton';
import DashCard from './DashCard';
import LayerGlyph from './LayerGlyph';
import styles from '../../dashboard.module.less';

export type IssueFilter = 'all' | HealthIssueCode;

const ISSUE_CODES: HealthIssueCode[] = ['missingDef', 'unusedDim', 'isolated'];
const PREVIEW = 8;

export function dashboardHealthPath(filter: IssueFilter = 'all'): string {
  const search = filter === 'all' ? '' : `?issue=${filter}`;
  return `/graph-center/dashboard${search}#health`;
}

function parseIssueFilter(raw: string | null): IssueFilter {
  return raw && (ISSUE_CODES as string[]).includes(raw) ? (raw as HealthIssueCode) : 'all';
}

export default function HealthIssueTable({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [expanded, setExpanded] = useState(false);

  const filter = parseIssueFilter(searchParams.get('issue'));
  const issues = useMemo(() => db.healthIssues(), [db]);
  const summary = useMemo(() => db.healthSummary(), [db]);

  const filtered = useMemo(
    () => (filter === 'all' ? issues : issues.filter((i) => i.issue === filter)),
    [issues, filter],
  );
  const visible = expanded ? filtered : filtered.slice(0, PREVIEW);
  const hidden = Math.max(0, filtered.length - PREVIEW);

  useEffect(() => {
    setExpanded(false);
  }, [filter]);

  useEffect(() => {
    if (location.hash !== '#health') return;
    document.getElementById('health')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [location.hash, location.search]);

  const setFilter = (next: IssueFilter) => {
    navigate(dashboardHealthPath(next), { replace: true });
  };

  const filters: { key: IssueFilter; label: string; count: number }[] = [
    { key: 'all', label: t('graphCenter.dashboard.issuesAll'), count: issues.length },
    {
      key: 'missingDef',
      label: t('graphCenter.dashboard.healthMissingDef'),
      count: summary.missingDef,
    },
    {
      key: 'unusedDim',
      label: t('graphCenter.dashboard.healthUnusedDim'),
      count: summary.unusedDim,
    },
    { key: 'isolated', label: t('graphCenter.dashboard.healthIsolated'), count: summary.isolated },
  ];

  const issueLabel = (code: HealthIssueCode) =>
    code === 'missingDef'
      ? t('graphCenter.dashboard.healthMissingDef')
      : code === 'unusedDim'
        ? t('graphCenter.dashboard.healthUnusedDim')
        : t('graphCenter.dashboard.healthIsolated');

  const issueChipClass = (code: HealthIssueCode) =>
    code === 'missingDef'
      ? styles.issueChipDef
      : code === 'unusedDim'
        ? styles.issueChipDim
        : styles.issueChipIso;

  return (
    <div id="health" className={styles.healthAnchor}>
      <DashCard
        title={t('graphCenter.dashboard.issuesTitle')}
        microLabel={t('graphCenter.dashboard.issuesMicro')}
        extra={
          <div className={styles.caFilter}>
            {filters.map((f) => (
              <button
                key={f.key}
                type="button"
                className={`${styles.caFilterBtn} ${filter === f.key ? styles.caFilterBtnActive : ''}`}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                <span className={styles.issueFilterCount}>{f.count}</span>
              </button>
            ))}
          </div>
        }
      >
        {filtered.length === 0 ? (
          <div className={styles.emptyBlock}>
            <span className={styles.dashedRing} />
            {t('graphCenter.dashboard.issuesEmpty')}
          </div>
        ) : (
          <>
            <ul className={styles.issueList}>
              {visible.map((row) => (
                <li key={`${row.issue}-${row.id}`} className={styles.issueRow}>
                  <button
                    type="button"
                    className={styles.issueMain}
                    onClick={() => navigate(`/graph-center/entity/${encodeURIComponent(row.id)}`)}
                  >
                    <LayerGlyph layer={row.layer} size={12} />
                    <span className={styles.issueName}>{row.label}</span>
                    <span className={styles.issueType}>{typeLabel(row.type)}</span>
                    <span className={`${styles.issueChip} ${issueChipClass(row.issue)}`}>
                      {issueLabel(row.issue)}
                    </span>
                    <span className={styles.issueRef}>
                      {t('graphCenter.relationCount', { num: row.refCount })}
                    </span>
                  </button>
                  <button
                    type="button"
                    className={styles.issueLocate}
                    title={t('graphCenter.locateTooltip')}
                    onClick={() =>
                      navigate(`/graph-center/global?locate=${encodeURIComponent(row.id)}`)
                    }
                  >
                    <CrosshairIcon size={13} />
                  </button>
                </li>
              ))}
            </ul>
            {hidden > 0 && (
              <button
                type="button"
                className={styles.issueExpand}
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded
                  ? t('graphCenter.dashboard.issuesCollapse')
                  : t('graphCenter.dashboard.issuesExpand', { num: filtered.length })}
              </button>
            )}
          </>
        )}
      </DashCard>
    </div>
  );
}
