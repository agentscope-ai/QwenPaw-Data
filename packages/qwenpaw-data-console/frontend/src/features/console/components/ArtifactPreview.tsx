import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { FileUnknownOutlined, LoadingOutlined, WarningOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { dataConsoleApi } from '../api/client';
import {
  formatJsonPreview,
  getArtifactPreviewKind,
  isInlinePreviewTooLarge,
  MAX_INLINE_TEXT_BYTES,
  parseCsvPreview,
  type ArtifactPreviewKind,
} from '../model/artifactPreview';
import { resolveArtifactAccess, workspacePreviewUrl } from '../model/artifacts';
import type { Artifact } from '../types';
import styles from '../index.module.less';

const INLINE_TEXT_KINDS = new Set<ArtifactPreviewKind>([
  'csv',
  'json',
  'markdown',
  'text',
]);
const SIZE_LIMITED_KINDS = new Set<ArtifactPreviewKind>([
  ...INLINE_TEXT_KINDS,
  'html',
]);
const LARGE_FILE_HINT = '文件超过 2 MB，为避免页面卡顿，请使用下载或新窗口打开。';

interface PreviewState {
  status: 'loading' | 'ready' | 'unsupported' | 'error';
  kind: ArtifactPreviewKind;
  url?: string;
  text?: string;
  error?: string;
}

interface ArtifactPreviewProps {
  artifact: Artifact;
  sessionId: string;
}

function PreviewPlaceholder({ icon, title, description }: {
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

export default function ArtifactPreview({ artifact, sessionId }: ArtifactPreviewProps) {
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
    if (SIZE_LIMITED_KINDS.has(nextKind) && isInlinePreviewTooLarge(artifact.sizeBytes)) {
      setPreview({
        status: 'unsupported',
        kind: nextKind,
        error: LARGE_FILE_HINT,
      });
      return () => abortController.abort();
    }
    if (nextKind === 'html' && artifact.access?.relativePath) {
      setPreview({
        status: 'ready',
        kind: nextKind,
        url: workspacePreviewUrl(sessionId, artifact.access.relativePath),
      });
      return () => abortController.abort();
    }

    setPreview({ status: 'loading', kind: nextKind });
    void (async () => {
      try {
        const access = await resolveArtifactAccess(
          sessionId,
          artifact,
          'preview',
          dataConsoleApi,
          undefined,
          SIZE_LIMITED_KINDS.has(nextKind) ? { maxBytes: MAX_INLINE_TEXT_BYTES } : undefined,
        );
        if (disposed) {
          access.release();
          return;
        }
        release = access.release;

        if (SIZE_LIMITED_KINDS.has(nextKind) && isInlinePreviewTooLarge(access.sizeBytes)) {
          if (!disposed) {
            setPreview({
              status: 'unsupported',
              kind: nextKind,
              error: LARGE_FILE_HINT,
            });
          }
          return;
        }
        if (INLINE_TEXT_KINDS.has(nextKind)) {
          const response = await fetch(access.url, { signal: abortController.signal });
          if (!response.ok) throw new Error(`加载失败（${response.status}）`);
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
  }, [artifact, sessionId]);

  const csv = useMemo(() => {
    if (preview.status !== 'ready' || preview.kind !== 'csv') return null;
    return parseCsvPreview(preview.text ?? '', artifact.name.toLowerCase().endsWith('.tsv') ? '\t' : ',');
  }, [artifact.name, preview]);

  if (preview.status === 'loading') {
    return (
      <PreviewPlaceholder
        icon={<LoadingOutlined spin />}
        title="正在加载预览"
        description="正在读取成果文件内容…"
      />
    );
  }
  if (preview.status === 'unsupported') {
    return (
      <PreviewPlaceholder
        icon={<FileUnknownOutlined />}
        title="暂不支持内嵌预览"
        description={preview.error ?? '该文件类型请使用下载或新窗口打开。'}
      />
    );
  }
  if (preview.status === 'error') {
    return (
      <PreviewPlaceholder
        icon={<WarningOutlined />}
        title="预览加载失败"
        description={preview.error ?? '请稍后重试，或使用下载和新窗口打开。'}
      />
    );
  }
  if (preview.kind === 'image') {
    return <div className={styles.artifactImagePreview}><img src={preview.url} alt={artifact.name} /></div>;
  }
  if (preview.kind === 'html') {
    return (
      <iframe
        className={styles.artifactDocumentPreview}
        src={preview.url}
        title={artifact.name}
        sandbox="allow-scripts allow-downloads"
      />
    );
  }
  if (preview.kind === 'pdf') {
    return <iframe className={styles.artifactDocumentPreview} src={preview.url} title={artifact.name} />;
  }
  if (preview.kind === 'audio') {
    return <div className={styles.artifactMediaPreview}><audio src={preview.url} controls /></div>;
  }
  if (preview.kind === 'video') {
    return <div className={styles.artifactMediaPreview}><video src={preview.url} controls /></div>;
  }
  if (preview.kind === 'markdown') {
    return <div className={styles.artifactMarkdownPreview}><Markdown content={preview.text ?? ''} /></div>;
  }
  if (preview.kind === 'csv' && csv) {
    const [header = [], ...rows] = csv.rows;
    return (
      <div className={styles.artifactCsvPreview}>
        <table>
          <thead><tr>{header.map((cell, index) => <th key={`${index}-${cell}`}>{cell || '—'}</th>)}</tr></thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {header.map((_, cellIndex) => <td key={cellIndex}>{row[cellIndex] ?? ''}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
        {csv.truncated ? <p>仅展示前 200 行，完整内容请下载查看。</p> : null}
      </div>
    );
  }
  return (
    <pre className={styles.artifactTextPreview}>
      {preview.kind === 'json' ? formatJsonPreview(preview.text ?? '') : preview.text}
    </pre>
  );
}
