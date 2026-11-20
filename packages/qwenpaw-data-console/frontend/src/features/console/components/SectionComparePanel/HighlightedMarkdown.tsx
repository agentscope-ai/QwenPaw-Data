import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import type { HighlightSpan } from '../../model/report/sectionDiff';
import highlightPlugin from './highlightPlugin';
import 'katex/dist/katex.min.css';
import styles from './index.module.less';

export default function HighlightedMarkdown({
  content,
  spans,
  activeId,
  onSelect,
}: {
  content: string;
  spans: HighlightSpan[];
  activeId: string | null;
  onSelect: (id: string) => void;
}) {
  const plugins = useMemo(
    () => [
      [highlightPlugin, { source: content, spans }] as [
        typeof highlightPlugin,
        { source: string; spans: HighlightSpan[] },
      ],
      rehypeKatex,
    ],
    [content, spans],
  );
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={plugins}
        components={{
          mark: ({ children, node }) => {
            const id = node?.properties['data-diff-id'];
            if (typeof id !== 'string') return <mark>{children}</mark>;
            return (
              <span
                data-diff-id={id}
                role="button"
                tabIndex={0}
                className={`${styles.diffHit} ${activeId === id ? styles.diffHitActive : ''}`}
                onClick={() => onSelect(id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onSelect(id);
                  }
                }}
              >
                {children}
              </span>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
