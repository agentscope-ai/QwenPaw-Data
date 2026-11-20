import { useEffect, useRef } from 'react';
import { renderAsync } from 'docx-preview';
import styles from './KGDocsPage.module.less';

export default function DocxPreview({
  data,
  className,
  onError,
}: {
  data: Blob;
  className?: string;
  onError?: (error: unknown) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let cancelled = false;
    container.replaceChildren();

    void renderAsync(data, container, undefined, {
      className: 'kg-docx',
      inWrapper: true,
      ignoreWidth: true,
      ignoreHeight: true,
      breakPages: false,
      renderHeaders: true,
      renderFooters: true,
      renderFootnotes: true,
      renderEndnotes: true,
      useBase64URL: true,
    }).catch((error) => {
      if (cancelled) return;
      console.error('Failed to render docx preview:', error);
      container.textContent = '';
      onError?.(error);
    });

    return () => {
      cancelled = true;
      container.replaceChildren();
    };
  }, [data]);

  return (
    <div className={className ?? styles.docxPreviewWrap}>
      <div ref={containerRef} className={styles.docxPreview} />
    </div>
  );
}
