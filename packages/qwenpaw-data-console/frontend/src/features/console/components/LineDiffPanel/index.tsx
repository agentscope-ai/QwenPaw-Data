import { Alert, Empty, Spin, Switch } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type { PointerEvent, UIEvent } from 'react';
import { useAppI18n } from '@/i18n';
import { dataConsoleApi } from '../../api/client';
import type { LineDiffResult } from '../../model/compare/lineDiff';
import { splitTextSpans } from '../../model/compare/textSpans';
import type { CompareSelection } from '../../model/report/sectionDiff';
import styles from './index.module.less';

export default function LineDiffPanel({
  sessionId,
  selection,
}: {
  sessionId: string;
  selection: CompareSelection;
}) {
  const { t } = useAppI18n();
  const { referencePath, targetPath } = selection;
  const [result, setResult] = useState<LineDiffResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sync, setSync] = useState(true);
  const [leftRatio, setLeftRatio] = useState(50);
  const columns = useRef<HTMLDivElement>(null);
  const left = useRef<HTMLDivElement>(null);
  const right = useRef<HTMLDivElement>(null);
  const mirroredTop = useRef(new WeakMap<HTMLDivElement, number>());

  useEffect(() => {
    let disposed = false;
    setResult(null);
    setError(null);
    setLoading(Boolean(referencePath));
    if (referencePath) {
      void dataConsoleApi
        .diffWorkspaceFiles(sessionId, {
          reference_path: referencePath,
          target_path: targetPath,
        })
        .then((value) => {
          if (!disposed) setResult(value);
        })
        .catch((reason: unknown) => {
          if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    }
    return () => {
      disposed = true;
    };
  }, [sessionId, referencePath, targetPath]);

  const onScroll = (event: UIEvent<HTMLDivElement>) => {
    const source = event.currentTarget;
    const expectedTop = mirroredTop.current.get(source);
    mirroredTop.current.delete(source);
    if (!sync || (expectedTop !== undefined && Math.abs(source.scrollTop - expectedTop) <= 1))
      return;
    const destination = source === left.current ? right.current : left.current;
    if (!destination) return;
    const extent = source.scrollHeight - source.clientHeight;
    const nextTop =
      extent > 0
        ? (source.scrollTop / extent) * (destination.scrollHeight - destination.clientHeight)
        : 0;
    // Do not write the originating pane back on the mirrored scroll event:
    // even assigning the same position cancels native smooth keyboard scrolling.
    if (Math.abs(destination.scrollTop - nextTop) > 1) {
      destination.scrollTop = nextTop;
      mirroredTop.current.set(destination, destination.scrollTop);
    }
  };

  const resize = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const rect = columns.current?.getBoundingClientRect();
    if (rect?.width)
      setLeftRatio(Math.max(20, Math.min(80, ((event.clientX - rect.left) / rect.width) * 100)));
  };
  const differences = result ? result.stats.insert + result.stats.delete + result.stats.replace : 0;

  if (!referencePath) return <Empty description={t('console.compareNoReference')} />;

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <label className={styles.sync}>
          <Switch size="small" checked={sync} onChange={setSync} />
          {t('console.compareSyncScroll')}
        </label>
        <strong title={selection.targetLabel}>{selection.targetLabel}</strong>
        {result ? (
          <span>
            {differences
              ? t('console.compareDiffSummary', { count: differences })
              : t('console.compareNoDiff')}
          </span>
        ) : null}
      </div>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      {result?.truncated ? (
        <Alert type="warning" showIcon message={t('console.compareTruncated')} />
      ) : null}
      {loading ? (
        <div className={styles.loading}>
          <Spin />
        </div>
      ) : null}
      {result ? (
        <div
          ref={columns}
          className={styles.columns}
          style={{
            gridTemplateColumns: `minmax(0, ${leftRatio}fr) 8px minmax(0, ${100 - leftRatio}fr)`,
          }}
        >
          {(['reference', 'target'] as const).map((side, sideIndex) => (
            <div key={side} className={styles.pane} style={{ gridColumn: sideIndex === 0 ? 1 : 3 }}>
              <div className={styles.heading}>
                <span>
                  {t(
                    side === 'reference'
                      ? 'console.compareReferenceSide'
                      : 'console.compareTargetSide',
                  )}
                </span>
                <strong
                  title={side === 'reference' ? selection.referenceLabel : selection.targetLabel}
                >
                  {side === 'reference' ? selection.referenceLabel : selection.targetLabel}
                </strong>
              </div>
              <div
                ref={side === 'reference' ? left : right}
                className={styles.scroll}
                onScroll={onScroll}
              >
                {result.rows.map((row, index) => {
                  const line = side === 'reference' ? row.ref_no : row.tgt_no;
                  const text = side === 'reference' ? row.ref_text : row.tgt_text;
                  const spans =
                    row.type === 'replace'
                      ? side === 'reference'
                        ? row.ref_spans
                        : row.tgt_spans
                      : [];
                  return (
                    <div
                      key={index}
                      className={`${styles.row} ${line === null ? styles.empty : styles[row.type]}`}
                    >
                      <span className={styles.number}>{line ?? ''}</span>
                      <code>
                        {splitTextSpans(text, spans).map((part) =>
                          part.highlighted ? <mark key={part.start}>{part.text}</mark> : part.text,
                        )}
                      </code>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          <div
            className={styles.separator}
            role="separator"
            tabIndex={0}
            aria-label={t('console.compareResize')}
            aria-orientation="vertical"
            aria-valuemin={20}
            aria-valuemax={80}
            aria-valuenow={Math.round(leftRatio)}
            onPointerDown={(event) => {
              event.preventDefault();
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={resize}
            onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                event.preventDefault();
                setLeftRatio((value) =>
                  Math.max(20, Math.min(80, value + (event.key === 'ArrowLeft' ? -2 : 2))),
                );
              }
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
