import { useEffect, useState, type ReactNode } from 'react';
import { CloseOutlined, FileUnknownOutlined, LoadingOutlined, WarningOutlined } from '@/design';
import { Button, Typography } from '@/design';
import { useTranslation } from 'react-i18next';
import { kgDocsApi, type KgDocument, type KgDocPreviewKind } from '@/services/kgDocs';
import FadePresence from '../GraphCenter/components/FadePresence';
import DocxPreview from './DocxPreview';
import MarkdownDoc from './MarkdownDoc';
import styles from './KGDocsPage.module.less';

const MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024;
const MAX_DOCX_PREVIEW_BYTES = 20 * 1024 * 1024;

type PreviewState =
  | { status: 'idle' }
  | { status: 'loading' }
  | {
      status: 'ready';
      kind: KgDocPreviewKind;
      text?: string;
      url?: string;
      blob?: Blob;
      truncated?: boolean;
    }
  | { status: 'error'; message: string }
  | { status: 'unsupported'; message: string };

function PreviewPlaceholder({
  icon,
  title,
  description,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className={styles.previewPlaceholder} role="status">
      {icon ? <span className={styles.previewPlaceholderIcon}>{icon}</span> : null}
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}

export default function DocPreviewPanel({
  doc,
  onClose,
}: {
  doc: KgDocument | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<PreviewState>({ status: 'idle' });
  const open = Boolean(doc);

  useEffect(() => {
    if (!doc) {
      setPreview({ status: 'idle' });
      return;
    }

    let disposed = false;
    let objectUrl: string | undefined;
    const abortController = new AbortController();
    const extension = doc.filename.split('.').pop()?.toLowerCase() ?? '';

    setPreview({ status: 'loading' });

    void (async () => {
      try {
        if (extension === 'docx' && doc.file_size > MAX_DOCX_PREVIEW_BYTES) {
          setPreview({
            status: 'unsupported',
            message: t('kgDocs.preview.tooLarge'),
          });
          return;
        }

        if (['txt', 'md'].includes(extension) && doc.file_size > MAX_INLINE_TEXT_BYTES) {
          setPreview({
            status: 'unsupported',
            message: t('kgDocs.preview.tooLarge'),
          });
          return;
        }

        if (extension === 'pdf') {
          const blob = await kgDocsApi.downloadKgDocBlob(doc.doc_id);
          if (disposed) return;
          objectUrl = URL.createObjectURL(blob);
          setPreview({ status: 'ready', kind: 'pdf', url: objectUrl });
          return;
        }

        if (extension === 'docx') {
          const blob = await kgDocsApi.downloadKgDocBlob(doc.doc_id);
          if (disposed) return;
          setPreview({ status: 'ready', kind: 'docx', blob });
          return;
        }

        if (extension === 'txt' || extension === 'md') {
          const blob = await kgDocsApi.downloadKgDocBlob(doc.doc_id);
          if (disposed) return;
          const text = await blob.text();
          setPreview({
            status: 'ready',
            kind: extension === 'md' ? 'markdown' : 'text',
            text,
          });
          return;
        }

        setPreview({
          status: 'unsupported',
          message: t('kgDocs.preview.unsupported'),
        });
      } catch (error) {
        if (disposed || abortController.signal.aborted) return;
        setPreview({
          status: 'error',
          message: error instanceof Error ? error.message : t('kgDocs.preview.failed'),
        });
      }
    })();

    return () => {
      disposed = true;
      abortController.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [doc, t]);

  return (
    <aside
      className={`${styles.previewDock} ${open ? styles.previewDockOpen : ''}`}
      aria-hidden={!open}
      aria-label={t('kgDocs.preview.title')}
    >
      <div className={styles.previewPanel}>
        <div className={styles.previewHeader}>
          <div className={styles.previewHeaderText}>
            <span className={styles.previewEyebrow}>{t('kgDocs.preview.title')}</span>
            {doc ? (
              <span title={doc.filename}>
                <Typography.Text className={styles.previewFilename} ellipsis>
                  {doc.filename}
                </Typography.Text>
              </span>
            ) : null}
          </div>
          <Button
            type="text"
            icon={<CloseOutlined />}
            aria-label={t('common.close')}
            onClick={onClose}
          />
        </div>
        <div className={styles.previewBody}>
          <FadePresence
            key={preview.status}
            open={preview.status !== 'idle'}
            kind="page"
            className={styles.previewBodyFade}
          >
            {preview.status === 'loading' ? (
              <PreviewPlaceholder
                icon={<LoadingOutlined spin />}
                title={t('kgDocs.preview.loading')}
                description={t('kgDocs.preview.loadingHint')}
              />
            ) : null}
            {preview.status === 'unsupported' ? (
              <PreviewPlaceholder
                icon={<FileUnknownOutlined />}
                title={t('kgDocs.preview.unsupportedTitle')}
                description={preview.message}
              />
            ) : null}
            {preview.status === 'error' ? (
              <PreviewPlaceholder
                icon={<WarningOutlined />}
                title={t('kgDocs.preview.failed')}
                description={preview.message}
              />
            ) : null}
            {preview.status === 'ready' && preview.kind === 'pdf' && preview.url ? (
              <iframe
                className={styles.previewFrame}
                src={preview.url}
                title={doc?.filename ?? t('kgDocs.preview.title')}
              />
            ) : null}
            {preview.status === 'ready' && preview.kind === 'docx' && preview.blob ? (
              <DocxPreview
                data={preview.blob}
                onError={(error) => {
                  setPreview({
                    status: 'error',
                    message: error instanceof Error ? error.message : t('kgDocs.preview.failed'),
                  });
                }}
              />
            ) : null}
            {preview.status === 'ready' && preview.kind === 'markdown' ? (
              <div className={styles.markdownPreviewWrap}>
                <MarkdownDoc source={preview.text ?? ''} />
                {preview.truncated ? (
                  <p className={styles.previewTruncated}>{t('kgDocs.preview.truncated')}</p>
                ) : null}
              </div>
            ) : null}
            {preview.status === 'ready' &&
            preview.kind !== 'pdf' &&
            preview.kind !== 'markdown' &&
            preview.kind !== 'docx' ? (
              <div className={styles.textPreviewWrap}>
                <pre className={styles.textPreview}>{preview.text}</pre>
                {preview.truncated ? (
                  <p className={styles.previewTruncated}>{t('kgDocs.preview.truncated')}</p>
                ) : null}
              </div>
            ) : null}
          </FadePresence>
        </div>
      </div>
    </aside>
  );
}
