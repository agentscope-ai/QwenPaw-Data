import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import type { Components } from 'react-markdown';
import { useAppI18n } from '@/i18n';
import {
  COMMENT_PREVIEW_CHANNEL,
  createCommentHtml,
} from '../../model/artifacts/commentPreviewBridge';
import { mountCommentPreview } from '../../model/artifacts/commentPreviewRuntime';
import { remarkCommentCompatibility } from '../../model/artifacts/commentMarkdownCompatibility';
import {
  mapMarkdownBlocks,
  type CommentBlock,
  type CommentPreviewResult,
  type CommentSelection,
} from '../../model/artifacts/commentSourceMap';
import 'katex/dist/katex.min.css';
import styles from './index.module.less';

const remarkPlugins = [remarkGfm, remarkMath, remarkCommentCompatibility];

function PositionedMarkdown({
  text,
  identity,
  selection,
  onResult,
}: {
  text: string;
  identity: string;
  selection: CommentSelection;
  onResult: (result: CommentPreviewResult) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const runtime = useRef<ReturnType<typeof mountCommentPreview>>();
  const blocks = useRef<CommentBlock[]>([]);
  const attribute = `data-fd-comment-${identity}`;
  const markup = useMemo(() => {
    function positions() {
      return (tree: unknown) => {
        blocks.current = mapMarkdownBlocks(tree, attribute, text);
      };
    }
    const components: Components = {
      pre: ({ node, children, ...props }) => {
        const start = node?.position?.start.offset;
        const end = node?.position?.end.offset;
        // Reuse the SDK's code renderer (including Mermaid) for the full original fence.
        return start !== undefined && end !== undefined ? (
          <div {...{ [attribute]: node?.properties[attribute] }}>
            <Markdown content={text.slice(start, end)} />
          </div>
        ) : (
          <pre {...props}>{children}</pre>
        );
      },
    };
    return (
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={[positions, rehypeKatex]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    );
  }, [text, attribute]);
  useEffect(() => {
    if (!root.current) return;
    const instance = mountCommentPreview(root.current, blocks.current, attribute, onResult);
    runtime.current = instance;
    return () => {
      instance.dispose();
      runtime.current = undefined;
    };
  }, [text, attribute, onResult]);
  useEffect(() => {
    runtime.current?.update(selection);
  }, [selection, text, attribute]);
  return (
    <div
      ref={root}
      className={`${styles.artifactMarkdownPreview} ${styles.commentRenderedBody} ${styles.commentMarkdown}`}
      tabIndex={0}
    >
      {markup}
    </div>
  );
}

function PositionedHtml({
  text,
  identity,
  name,
  selection,
  onResult,
}: {
  text: string;
  identity: string;
  name: string;
  selection: CommentSelection;
  onResult: (result: CommentPreviewResult) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const latest = useRef(selection);
  latest.current = selection;
  const html = useMemo(() => createCommentHtml(text, identity), [text, identity]);
  const send = useCallback(() => {
    frame.current?.contentWindow?.postMessage(
      { channel: COMMENT_PREVIEW_CHANNEL, identity, kind: 'select', ...latest.current },
      '*',
    );
  }, [identity]);
  useEffect(() => {
    function receive(event: MessageEvent) {
      const data = event.data;
      if (
        event.source !== frame.current?.contentWindow ||
        !data ||
        data.channel !== COMMENT_PREVIEW_CHANNEL ||
        data.identity !== identity
      )
        return;
      if (data.kind === 'ready') send();
      if (
        data.kind === 'result' &&
        data.sequence === latest.current.sequence &&
        Number.isSafeInteger(data.count) &&
        data.count >= 0
      )
        onResult({ sequence: data.sequence, count: data.count });
    }
    window.addEventListener('message', receive);
    send();
    return () => window.removeEventListener('message', receive);
  }, [identity, send, onResult]);
  useEffect(send, [selection, send]);
  return (
    <iframe
      ref={frame}
      srcDoc={html}
      title={name}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      className={`${styles.artifactDocumentPreview} ${styles.commentRenderedBody}`}
      onLoad={send}
    />
  );
}

export default memo(function RenderedCommentPreview({
  kind,
  text,
  name,
  selection,
}: {
  kind: 'html' | 'markdown';
  text: string;
  name: string;
  selection: CommentSelection;
}) {
  const { t } = useAppI18n();
  // A new document identity invalidates delayed responses even when paths are reused.
  const identity = useMemo(() => crypto.randomUUID().replace(/-/g, ''), [text, kind]);
  const [result, setResult] = useState<CommentPreviewResult | null>(null);
  const onResult = useCallback((next: CommentPreviewResult) => setResult(next), []);
  const hasMatch = result?.sequence === selection.sequence && result.count > 0;
  return (
    <div className={styles.commentRenderedPreview}>
      {kind === 'html' ? (
        <PositionedHtml
          key={identity}
          text={text}
          name={name}
          identity={identity}
          selection={selection}
          onResult={onResult}
        />
      ) : (
        <PositionedMarkdown
          key={identity}
          text={text}
          identity={identity}
          selection={selection}
          onResult={onResult}
        />
      )}
      <div className={styles.commentRenderedStatus} role="status">
        {selection.range
          ? hasMatch
            ? t('artifact.commentLocated')
            : t('artifact.commentUnmapped')
          : t('artifact.commentScrollOnly')}
      </div>
    </div>
  );
});
