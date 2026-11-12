import Markdown from '@agentscope-ai/chat/lib/Markdown';
import {
  DeleteOutlined,
  FileUnknownOutlined,
  LoadingOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Button, Input } from '@agentscope-ai/design';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { dataConsoleApi } from '../../api/client';
import {
  artifactFilePath,
  isArtifactSplitCommentPreview,
  type ArtifactCommentHighlight,
  type PendingArtifactComment,
} from '../../model/artifacts/artifactComments';
import {
  formatJsonPreview,
  getArtifactPreviewKind,
  isCommentablePreviewKind,
  MAX_INLINE_TEXT_BYTES,
  parseCsvPreview,
  type ArtifactPreviewKind,
} from '../../model/artifacts/artifactPreview';
import { resolveArtifactAccess } from '../../model/artifacts/artifacts';
import type { Artifact } from '../../types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';
import RenderedCommentPreview from './RenderedCommentPreview';
import type { SourceRange } from '../../model/artifacts/commentSourceMap';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface PreviewState {
  status: 'loading' | 'ready' | 'unsupported' | 'error';
  kind: ArtifactPreviewKind;
  url?: string;
  text?: string;
  error?: string;
}

interface ArtifactPreviewProps {
  artifact: Artifact;
  commentMode?: boolean;
  highlight?: ArtifactCommentHighlight | null;
  onComment?: (comment: PendingArtifactComment) => void;
  sessionId: string;
}

function PreviewPlaceholder({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className={styles.artifactPreviewPlaceholder} role="status">
      <span>{icon}</span>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}

function selectedLine(node: Node | null, root: HTMLElement) {
  const element = node instanceof Element ? node : node?.parentElement;
  const row = element?.closest<HTMLElement>('[data-line]');
  if (!row || !root.contains(row)) return null;
  const line = Number(row.dataset.line);
  return Number.isInteger(line) ? line : null;
}

function selectedLineRange(root: HTMLElement) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;
  const anchor = selectedLine(selection.anchorNode, root);
  const focus = selectedLine(selection.focusNode, root);
  if (anchor === null || focus === null) return null;
  return { start: Math.min(anchor, focus), end: Math.max(anchor, focus) };
}

function LineCommentPreview({
  highlight,
  onComment,
  onRangeChange,
  path,
  text,
}: {
  highlight?: ArtifactCommentHighlight | null;
  onComment?: (comment: PendingArtifactComment) => void;
  onRangeChange?: (range: SourceRange | null) => void;
  path: string;
  text: string;
}) {
  const { t } = useAppI18n();
  const lines = useMemo(() => text.split('\n'), [text]);
  const listRef = useRef<HTMLDivElement>(null);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setDraft('');
    setEditing(false);
    const initial = highlight ? { start: highlight.lineStart, end: highlight.lineEnd } : null;
    setRange(initial);
    onRangeChange?.(initial);
  }, [highlight, path, text, onRangeChange]);

  useEffect(() => {
    if (!highlight) return;
    const row = listRef.current?.querySelector(`[data-line="${highlight.lineStart}"]`);
    row?.scrollIntoView({ block: 'center' });
  }, [highlight, text]);

  const handleNativeSelection = () => {
    if (!onComment || !listRef.current) return;
    const nextRange = selectedLineRange(listRef.current);
    if (!nextRange) return;
    setRange(nextRange);
    onRangeChange?.(nextRange);
    setDraft('');
    setEditing(true);
  };

  const clearSelection = () => {
    window.getSelection()?.removeAllRanges();
    setRange(null);
    onRangeChange?.(null);
    setDraft('');
    setEditing(false);
  };

  const submit = () => {
    if (!onComment || !range || !draft.trim()) return;
    onComment({
      path,
      line_start: range.start,
      line_end: range.end,
      comment: draft.trim(),
    });
    clearSelection();
  };

  return (
    <div className={styles.artifactLinePreview}>
      <div className={styles.artifactLineHint}>
        <span>{t('artifact.commentSelectStart')}</span>
        {range ? (
          <span className={styles.artifactLineSelectionStatus}>
            {t('artifact.commentSelectedRange', {
              start: range.start,
              end: range.end,
            })}
            {onComment ? (
              <Button
                aria-label={t('artifact.commentClearSelection')}
                className={styles.artifactLineClearButton}
                danger
                icon={<DeleteOutlined />}
                size="small"
                title={t('artifact.commentClearSelection')}
                type="text"
                onClick={clearSelection}
              />
            ) : null}
          </span>
        ) : null}
      </div>
      <div
        aria-label={t('artifact.commentSelectStart')}
        className={styles.artifactLineList}
        ref={listRef}
        onMouseUp={handleNativeSelection}
      >
        {lines.map((line, index) => {
          const number = index + 1;
          const selected = Boolean(range && number >= range.start && number <= range.end);
          return (
            <div
              className={`${styles.artifactLineRow} ${
                selected ? styles.artifactLineRowSelected : ''
              }`}
              data-line={number}
              key={number}
            >
              <span>{number}</span>
              <code>{line || ' '}</code>
            </div>
          );
        })}
      </div>
      {highlight?.comment && !editing ? (
        <p className={styles.artifactLineNote}>{highlight.comment}</p>
      ) : null}
      {onComment && range && editing ? (
        <div className={styles.artifactLineComposer}>
          <Input.TextArea
            autoSize={{ minRows: 2, maxRows: 5 }}
            placeholder={t('artifact.commentPlaceholder')}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <div>
            <span>{t('artifact.commentInputHint')}</span>
            <Button disabled={!draft.trim()} type="primary" onClick={submit}>
              {t('artifact.commentAddToInput')}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SplitCommentPreview({
  text,
  kind,
  name,
  path,
  highlight,
  onComment,
}: {
  text: string;
  kind: 'html' | 'markdown';
  name: string;
  path: string;
  highlight?: ArtifactCommentHighlight | null;
  onComment?: (comment: PendingArtifactComment) => void;
}) {
  const { t } = useAppI18n();
  const [selection, setSelection] = useState<{
    range: SourceRange | null;
    sequence: number;
    text: string;
  }>({ range: null, sequence: 0, text });
  const onRangeChange = useCallback(
    (range: SourceRange | null) => {
      setSelection((previous) => ({ range, sequence: previous.sequence + 1, text }));
    },
    [text],
  );
  const current = useMemo(
    () => ({
      range: selection.text === text ? selection.range : null,
      sequence: selection.sequence,
    }),
    [selection, text],
  );
  return (
    <div className={styles.artifactSplitPreview}>
      <section className={styles.artifactSplitPane} aria-label={t('artifact.commentSource')}>
        <div className={styles.artifactSplitHeading}>{t('artifact.commentSource')}</div>
        <LineCommentPreview
          text={text}
          path={path}
          highlight={highlight}
          onComment={onComment}
          onRangeChange={onRangeChange}
        />
      </section>
      <section className={styles.artifactSplitPane} aria-label={t('artifact.commentRendered')}>
        <div className={styles.artifactSplitHeading}>{t('artifact.commentRendered')}</div>
        <RenderedCommentPreview kind={kind} text={text} name={name} selection={current} />
      </section>
    </div>
  );
}

export default function ArtifactPreview({
  artifact,
  commentMode = false,
  highlight,
  onComment,
  sessionId,
}: ArtifactPreviewProps) {
  const { t } = useAppI18n();
  const kind = getArtifactPreviewKind(artifact);
  const [preview, setPreview] = useState<PreviewState>({ status: 'loading', kind });

  useEffect(() => {
    let disposed = false;
    let release: () => void = () => undefined;
    const abortController = new AbortController();
    const nextKind = getArtifactPreviewKind(artifact);

    if (nextKind === 'unsupported') {
      setPreview({ status: 'unsupported', kind: nextKind });
      return () => abortController.abort();
    }
    if (
      ['csv', 'html', 'json', 'markdown', 'text'].includes(nextKind) &&
      artifact.sizeBytes !== undefined &&
      artifact.sizeBytes > MAX_INLINE_TEXT_BYTES
    ) {
      setPreview({
        status: 'unsupported',
        kind: nextKind,
        error: t('artifact.tooLarge'),
      });
      return () => abortController.abort();
    }

    setPreview({ status: 'loading', kind: nextKind });
    void (async () => {
      try {
        const access = await resolveArtifactAccess(sessionId, artifact, 'preview', dataConsoleApi);
        if (disposed) {
          access.release();
          return;
        }
        release = access.release;

        if (nextKind === 'html' && access.sourceText !== undefined) {
          setPreview({ status: 'ready', kind: nextKind, text: access.sourceText, url: access.url });
        } else if (['csv', 'json', 'markdown', 'text'].includes(nextKind)) {
          const response = await fetch(access.url, { signal: abortController.signal });
          if (!response.ok) {
            throw new Error(t('artifact.loadFailedWithStatus', { status: response.status }));
          }
          const text = await response.text();
          if (!disposed) setPreview({ status: 'ready', kind: nextKind, text });
        } else {
          setPreview({ status: 'ready', kind: nextKind, url: access.url });
        }
      } catch (error) {
        if (!disposed && !abortController.signal.aborted) {
          setPreview({
            status: 'error',
            kind: nextKind,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }
    })();

    return () => {
      disposed = true;
      abortController.abort();
      release();
    };
  }, [artifact, sessionId, t]);

  const csv = useMemo(() => {
    if (preview.status !== 'ready' || preview.kind !== 'csv') return null;
    return parseCsvPreview(
      preview.text ?? '',
      artifact.name.toLowerCase().endsWith('.tsv') ? '\t' : ',',
    );
  }, [artifact.name, preview]);

  if (preview.status === 'loading') {
    return (
      <PreviewPlaceholder
        icon={<LoadingOutlined spin />}
        title={t('artifact.loadingPreview')}
        description={t('artifact.reading')}
      />
    );
  }
  if (preview.status === 'unsupported') {
    return (
      <PreviewPlaceholder
        icon={<FileUnknownOutlined />}
        title={t('artifact.unsupported')}
        description={preview.error ?? t('artifact.unsupportedDescription')}
      />
    );
  }
  if (preview.status === 'error') {
    return (
      <PreviewPlaceholder
        icon={<WarningOutlined />}
        title={t('artifact.previewFailed')}
        description={preview.error ?? t('artifact.previewFailedDescription')}
      />
    );
  }
  const path = artifactFilePath(artifact);
  const renderedPreview =
    preview.kind === 'html' ? (
      <iframe
        className={styles.artifactDocumentPreview}
        src={preview.url}
        title={artifact.name}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
      />
    ) : preview.kind === 'markdown' ? (
      <div className={styles.artifactMarkdownPreview}>
        <Markdown content={preview.text ?? ''} />
      </div>
    ) : null;

  if (commentMode && isCommentablePreviewKind(preview.kind) && path) {
    const sourcePreview = (
      <LineCommentPreview
        highlight={highlight}
        onComment={onComment}
        path={path}
        text={preview.text ?? ''}
      />
    );
    if (
      isArtifactSplitCommentPreview(artifact, commentMode) &&
      (preview.kind === 'html' || preview.kind === 'markdown')
    ) {
      return (
        <SplitCommentPreview
          key={`${sessionId}:${path}`}
          text={preview.text ?? ''}
          kind={preview.kind}
          name={artifact.name}
          path={path}
          highlight={highlight}
          onComment={onComment}
        />
      );
    }
    return sourcePreview;
  }
  if (renderedPreview) return renderedPreview;
  if (preview.kind === 'image') {
    return (
      <div className={styles.artifactImagePreview}>
        <img src={preview.url} alt={artifact.name} />
      </div>
    );
  }
  if (preview.kind === 'pdf') {
    return (
      <iframe className={styles.artifactDocumentPreview} src={preview.url} title={artifact.name} />
    );
  }
  if (preview.kind === 'audio') {
    return (
      <div className={styles.artifactMediaPreview}>
        <audio src={preview.url} controls />
      </div>
    );
  }
  if (preview.kind === 'video') {
    return (
      <div className={styles.artifactMediaPreview}>
        <video src={preview.url} controls />
      </div>
    );
  }
  if (preview.kind === 'csv' && csv) {
    const [header = [], ...rows] = csv.rows;
    return (
      <div className={styles.artifactCsvPreview}>
        <table>
          <thead>
            <tr>
              {header.map((cell, index) => (
                <th key={`${index}-${cell}`}>{cell || '—'}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {header.map((_, cellIndex) => (
                  <td key={cellIndex}>{row[cellIndex] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {csv.truncated ? <p>{t('artifact.csvTruncated')}</p> : null}
      </div>
    );
  }
  return (
    <pre className={styles.artifactTextPreview}>
      {preview.kind === 'json' ? formatJsonPreview(preview.text ?? '') : preview.text}
    </pre>
  );
}
