import { parse } from 'parse5';

export interface SourceRange {
  start: number;
  end: number;
}
export interface CommentBlock {
  id: string;
  ranges: SourceRange[];
}
export interface CommentSelection {
  range: SourceRange | null;
  sequence: number;
}
export interface CommentPreviewResult {
  sequence: number;
  count: number;
}

interface Position {
  start: { line: number; column?: number; offset?: number };
  end: { line: number; column?: number; offset?: number };
}
interface MarkdownNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: MarkdownNode[];
  position?: Position;
}

function sourceLineAt(source: string) {
  const starts = [0];
  for (let i = 0; i < source.length; i++) if (source[i] === '\n') starts.push(i + 1);
  return (offset: number) => {
    let low = 0;
    let high = starts.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (starts[middle] <= offset) low = middle + 1;
      else high = middle;
    }
    return Math.max(1, low);
  };
}

/** HAST positions still refer to the original Markdown, before math/code rendering. */
export function mapMarkdownBlocks(
  tree: unknown,
  attribute: string,
  source: string,
): CommentBlock[] {
  const blocks: CommentBlock[] = [];
  const lineAt = sourceLineAt(source);
  const listLabels = new WeakSet<MarkdownNode>();
  const tags = new Set([
    'p',
    'li',
    'blockquote',
    'pre',
    'td',
    'th',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'hr',
    'img',
    'div',
  ]);
  function visit(node: MarkdownNode) {
    if (
      node.tagName === 'li' &&
      node.children?.some((child) => ['ul', 'ol'].includes(child.tagName ?? ''))
    ) {
      // Tight lists have bare inline content instead of a paragraph. Give that
      // content its own block so leaf-only highlighting does not discard it
      // when a nested list item also matches the selection.
      const children: MarkdownNode[] = [];
      let inline: MarkdownNode[] = [];
      const flush = () => {
        const positioned = inline.filter((child) => child.position);
        if (positioned.length) {
          const label: MarkdownNode = {
            type: 'element',
            tagName: 'div',
            properties: {},
            children: inline,
            position: {
              start: positioned[0].position!.start,
              end: positioned[positioned.length - 1].position!.end,
            },
          };
          listLabels.add(label);
          children.push(label);
        } else children.push(...inline);
        inline = [];
      };
      const phrasing = new Set(['a', 'em', 'strong', 'del', 'code', 'br', 'img', 'input', 'span']);
      for (const child of node.children) {
        if (child.type === 'text' || phrasing.has(child.tagName ?? '')) inline.push(child);
        else {
          flush();
          children.push(child);
        }
      }
      flush();
      node.children = children;
    }
    if (node.tagName && tags.has(node.tagName) && node.position) {
      const { start, end } = node.position;
      const id = String(blocks.length);
      node.properties = { ...node.properties, [attribute]: id };
      blocks.push({
        id,
        ranges: [
          {
            start: start.offset === undefined ? start.line : lineAt(start.offset),
            end:
              end.offset === undefined
                ? Math.max(start.line, end.line - (end.column === 1 ? 1 : 0))
                : lineAt(Math.max(start.offset ?? 0, end.offset - 1)),
          },
        ],
      });
      if (listLabels.has(node)) return;
      // Code/KaTeX/Mermaid are atomic visible blocks, not individual generated nodes.
      if (node.tagName === 'pre') {
        const classes = node.children?.[0]?.properties?.className;
        if (Array.isArray(classes) && classes.includes('math-display')) {
          // KaTeX replaces the pre node. Keep the source marker on a stable outer block.
          const original = { ...node, properties: { ...node.properties } };
          delete original.properties[attribute];
          node.tagName = 'div';
          node.children = [original];
        }
        return;
      }
    }
    node.children?.forEach(visit);
  }
  visit(tree as MarkdownNode);
  return blocks;
}

/** Insert preview markers at original offsets; never serialize or reformat the document. */
export function mapHtmlBlocks(
  source: string,
  attribute: string,
): { html: string; blocks: CommentBlock[]; bootstrapOffset: number } {
  if (!/^data-fd-comment-[a-z0-9]+$/.test(attribute)) throw new Error('Invalid preview attribute');
  const tree = parse(source, { sourceCodeLocationInfo: true });
  const lineAt = sourceLineAt(source);
  const blocks: CommentBlock[] = [];
  const edits: { offset: number; value: string }[] = [];
  let bootstrapOffset = 0;
  const excluded = new Set([
    'head',
    'script',
    'style',
    'template',
    'noscript',
    'title',
    'meta',
    'link',
    'iframe',
  ]);
  type Node = typeof tree | (typeof tree.childNodes)[number];
  function visit(node: Node) {
    if (node.nodeName === '#documentType' && node.sourceCodeLocation)
      bootstrapOffset = node.sourceCodeLocation.endOffset;
    if (
      'tagName' in node &&
      ['html', 'head'].includes(node.tagName) &&
      node.sourceCodeLocation?.startTag
    )
      bootstrapOffset = node.sourceCodeLocation.startTag.endOffset;
    if ('tagName' in node && excluded.has(node.tagName)) return;
    if ('tagName' in node && !['html', 'body'].includes(node.tagName)) {
      const location = node.sourceCodeLocation;
      if (location?.startTag) {
        const id = String(blocks.length);
        const ranges: SourceRange[] = [];
        const addLines = (start: number, end: number, target = ranges) => {
          source
            .slice(start, end)
            .split('\n')
            .forEach((line, index) => {
              if (line.trim())
                target.push({ start: lineAt(start) + index, end: lineAt(start) + index });
            });
        };
        addLines(location.startTag.startOffset, location.startTag.endOffset);
        if (location.endTag) addLines(location.endTag.startOffset, location.endTag.endOffset);
        for (const child of node.childNodes) {
          if (child.nodeName === '#text' && child.sourceCodeLocation) {
            const loc = child.sourceCodeLocation;
            addLines(loc.startOffset, loc.endOffset);
          }
        }
        // Insert immediately after the tag name, including unquoted-attribute documents.
        const name = /^<[^\s/>]+/.exec(source.slice(location.startTag.startOffset));
        if (name) {
          edits.push({
            offset: location.startTag.startOffset + name[0].length,
            value: ` ${attribute}="${id}"`,
          });
          blocks.push({ id, ranges });
          if (
            node.tagName === 'li' &&
            node.childNodes.some((child) => 'tagName' in child && !excluded.has(child.tagName))
          ) {
            // A list item may own bare text as well as matching child elements.
            // Mark its text separately so leaf filtering preserves the label.
            // Inline spans preserve the report's list structure and spacing.
            for (const child of node.childNodes) {
              if (child.nodeName !== '#text' || !child.sourceCodeLocation) continue;
              const { startOffset, endOffset } = child.sourceCodeLocation;
              if (!source.slice(startOffset, endOffset).trim()) continue;
              const textId = String(blocks.length);
              const textRanges: SourceRange[] = [];
              addLines(startOffset, endOffset, textRanges);
              blocks.push({ id: textId, ranges: textRanges });
              edits.push(
                { offset: startOffset, value: `<span ${attribute}="${textId}">` },
                { offset: endOffset, value: '</span>' },
              );
            }
          }
        }
      }
    }
    if ('childNodes' in node) node.childNodes.forEach(visit);
  }
  visit(tree);
  let html = source;
  for (const edit of edits.sort((a, b) => b.offset - a.offset))
    html = html.slice(0, edit.offset) + edit.value + html.slice(edit.offset);
  return { html, blocks, bootstrapOffset };
}
