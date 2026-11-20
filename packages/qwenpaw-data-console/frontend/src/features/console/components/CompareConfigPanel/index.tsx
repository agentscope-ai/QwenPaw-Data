import { Button, Empty, Select, Spin } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useAppI18n } from '@/i18n';
import { dataConsoleApi } from '../../api/client';
import type { CompareRecommendationItem } from '../../api/types';
import { compareFamily } from '../../model/compare/families';
import { selectedComparison } from '../../model/compare/selection';
import type { CompareSelection } from '../../model/report/sectionDiff';
import styles from './index.module.less';

/**
 * The "compare" picker stage, rendered inside a small standalone modal.
 *
 * Clicking 比较 opens this picker rather than the two-pane diff directly. The
 * compared document (target) is locked to the artifact the picker was opened
 * from; only the reference document is a real choice, and its candidates are
 * filtered to the same file family (markdown/csv/sql/json) so the pair is
 * always line-comparable. A short list of backend recommendations (2–3 pairs,
 * chosen by the model from the candidate path list) offers common shortcuts —
 * clicking one simply moves the reference selection. Confirming hands a
 * `CompareSelection` back to the Console, which swaps the preview modal body
 * for `SectionComparePanel`.
 */

export interface CompareCandidate {
  path: string;
  name: string;
}

interface CompareConfigPanelProps {
  sessionId: string | null;
  candidates: CompareCandidate[];
  initial: CompareSelection;
  onStart: (selection: CompareSelection) => void;
}

function baseName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

export default function CompareConfigPanel({
  sessionId,
  candidates,
  initial,
  onStart,
}: CompareConfigPanelProps) {
  const { t } = useAppI18n();
  // The target is the document the picker was opened from; it never moves.
  const targetPath = initial.targetPath;
  const targetFamily = useMemo(() => compareFamily(targetPath), [targetPath]);

  // Reference candidates must share the target's family and be a different file.
  const referenceCandidates = useMemo(
    () =>
      candidates.filter(
        (candidate) =>
          targetFamily !== null &&
          candidate.path !== targetPath &&
          compareFamily(candidate.path) === targetFamily,
      ),
    [candidates, targetPath, targetFamily],
  );

  const [referencePath, setReferencePath] = useState<string | null>(initial.referencePath);
  const [recommendations, setRecommendations] = useState<CompareRecommendationItem[]>([]);
  const [recommending, setRecommending] = useState(false);

  // A freshly opened picker always reflects the current document; drop any
  // pre-filled reference that is not a valid same-family candidate.
  useEffect(() => {
    const valid = referenceCandidates.some((candidate) => candidate.path === initial.referencePath);
    setReferencePath(valid ? initial.referencePath : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial.referencePath, targetPath]);

  // A stable identity for the candidate set. `candidates` is rebuilt on every
  // chat stream event while a session runs, so depending on the array itself
  // would reset and refetch the recommendations on each event, making the
  // shortcut cards flicker or vanish mid-run. Keying on the path/name contents
  // fires the fetch only when the actual candidate set changes.
  const referenceCandidateKey = useMemo(
    () => JSON.stringify(referenceCandidates.map(({ path, name }) => [path, name])),
    [referenceCandidates],
  );

  useEffect(() => {
    setReferencePath((current) =>
      referenceCandidates.some((candidate) => candidate.path === current) ? current : null,
    );
  }, [referenceCandidates]);

  // Ask the backend for 2–3 recommended reference documents. Purely advisory:
  // any failure just leaves the picker without shortcut cards.
  useEffect(() => {
    setRecommendations([]);
    setRecommending(false);
    if (!sessionId || !targetPath || referenceCandidates.length < 1) return undefined;
    let disposed = false;
    setRecommending(true);
    void dataConsoleApi
      .fetchCompareRecommendations(sessionId, {
        current_path: targetPath,
        candidates: referenceCandidates.map(({ path, name }) => ({ path, name })),
      })
      .then((result) => {
        if (disposed) return;
        setRecommendations(result.recommendations ?? []);
      })
      .catch(() => {
        if (!disposed) setRecommendations([]);
      })
      .finally(() => {
        if (!disposed) setRecommending(false);
      });
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, targetPath, referenceCandidateKey]);

  const options = useMemo(
    () =>
      referenceCandidates.map((candidate) => ({
        value: candidate.path,
        label: candidate.name,
      })),
    [referenceCandidates],
  );

  const labelOf = (path: string | null): string => {
    if (!path) return '';
    return candidates.find((candidate) => candidate.path === path)?.name ?? baseName(path);
  };

  // A recommendation only ever moves the reference; the target stays locked.
  const applyRecommendation = (recommendation: CompareRecommendationItem) => {
    if (referenceCandidates.some((candidate) => candidate.path === recommendation.reference_path)) {
      setReferencePath(recommendation.reference_path);
    }
  };

  const handleStart = () => {
    if (
      !targetPath ||
      !referencePath ||
      !referenceCandidates.some((candidate) => candidate.path === referencePath)
    )
      return;
    // Keep the on-disk comparison.json shortcut only when the pair is exactly the
    // recommended reproduction draft against its own GT; any other pair is a
    // generic recompute through the endpoint, so it carries no section.
    onStart(selectedComparison(initial, referencePath, labelOf(referencePath)));
  };

  const canStart = Boolean(
    targetPath &&
    referencePath &&
    referenceCandidates.some((candidate) => candidate.path === referencePath),
  );

  return (
    <div className={styles.panel}>
      <h3 className={styles.title}>{t('console.compareConfigTitle')}</h3>

      <div className={styles.fields}>
        <div className={styles.field}>
          <span className={styles.fieldLabel}>{t('console.compareReference')}</span>
          <Select
            className={styles.select}
            showSearch
            optionFilterProp="label"
            placeholder={t('console.compareSelectPlaceholder')}
            value={referencePath ?? undefined}
            options={options}
            onChange={(value) => setReferencePath(value ?? null)}
            notFoundContent={
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('console.compareNoCandidates')}
              />
            }
          />
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>{t('console.compareTarget')}</span>
          {/* The compared document is fixed to the artifact the picker was
              opened from; only the reference above is a choice. */}
          <Select
            className={styles.select}
            disabled
            value={targetPath || undefined}
            options={[{ value: targetPath, label: labelOf(targetPath) || targetPath }]}
          />
          <span className={styles.lockedHint}>{t('console.compareTargetLocked')}</span>
        </div>
      </div>

      {recommending || recommendations.length ? (
        <div className={styles.recommendations}>
          <span className={styles.recommendationsLabel}>{t('console.compareRecommended')}</span>
          {recommending ? (
            <div className={styles.recommendationLoading}>
              <Spin size="small" />
            </div>
          ) : (
            <div className={styles.recommendationCards}>
              {recommendations.map((recommendation) => (
                <button
                  key={recommendation.key}
                  type="button"
                  className={`${styles.recommendationCard} ${
                    referencePath === recommendation.reference_path
                      ? styles.recommendationCardActive
                      : ''
                  }`}
                  onClick={() => applyRecommendation(recommendation)}
                >
                  <strong>{recommendation.label}</strong>
                  {recommendation.description ? <span>{recommendation.description}</span> : null}
                </button>
              ))}
            </div>
          )}
        </div>
      ) : null}

      <div className={styles.actions}>
        <Button type="primary" disabled={!canStart} onClick={handleStart}>
          {t('console.compareStart')}
        </Button>
      </div>
    </div>
  );
}
