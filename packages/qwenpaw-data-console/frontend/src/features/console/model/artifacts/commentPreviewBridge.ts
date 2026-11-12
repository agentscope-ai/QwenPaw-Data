import { mapHtmlBlocks, type CommentBlock } from './commentSourceMap.js';
import { mountCommentPreview } from './commentPreviewRuntime.js';
import { injectEchartsResizeSupport } from './artifactPreview.js';

export const COMMENT_PREVIEW_CHANNEL = 'qwenpaw-data-comment-preview-v1';

/** Runs before report scripts; only the current parent may apply source ranges. */
function installBridge(
  mount: typeof mountCommentPreview,
  blocks: CommentBlock[],
  attribute: string,
  identity: string,
  channel: string,
) {
  const runtime = mount(
    document.documentElement,
    blocks,
    attribute,
    (result) => {
      window.parent.postMessage({ channel, identity, kind: 'result', ...result }, '*');
    },
    true,
  );
  window.addEventListener('message', (event) => {
    const data = event.data;
    if (
      event.source !== window.parent ||
      !data ||
      data.channel !== channel ||
      data.identity !== identity ||
      data.kind !== 'select'
    )
      return;
    runtime.update({ range: data.range, sequence: data.sequence });
  });
  window.parent.postMessage({ channel, identity, kind: 'ready' }, '*');
}

export function createCommentHtml(source: string, identity: string) {
  if (!/^[a-z0-9]+$/.test(identity)) throw new Error('Invalid preview identity');
  const attribute = `data-fd-comment-${identity}`;
  const { html, blocks, bootstrapOffset } = mapHtmlBlocks(source, attribute);
  const args = JSON.stringify([blocks, attribute, identity, COMMENT_PREVIEW_CHANNEL]).replace(
    /</g,
    '\\u003c',
  );
  const script = `<script>(${installBridge.toString()})(${mountCommentPreview.toString()}, ...${args});</script>`;
  return injectEchartsResizeSupport(
    html.slice(0, bootstrapOffset) + script + html.slice(bootstrapOffset),
  );
}
