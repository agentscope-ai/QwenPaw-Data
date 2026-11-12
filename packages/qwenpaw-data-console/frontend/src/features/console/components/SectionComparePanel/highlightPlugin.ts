import { decodeNamedCharacterReference } from 'decode-named-character-reference';
import type { HighlightSpan } from '../../model/report/sectionDiff.js';

interface TreeNode {
  type: string; // HAST node kind.
  tagName?: string; // Element name.
  value?: string; // Decoded text.
  properties?: Record<string, unknown>; // React-safe element properties.
  children?: TreeNode[]; // Nested nodes.
  position?: { start: { offset?: number }; end: { offset?: number } }; // Original source offsets.
}

/** Map original Markdown boundaries to decoded text without searching its values. */
export function decodedBoundaries(source: string, value: string): Map<number, number> | null {
  const map = new Map<number, number>([[0, 0]]);
  let decoded = '';
  for (let i = 0; i < source.length;) {
    let raw = source[i];
    let piece = raw;
    const escape = /^\\([!"#$%&'()*+,\-./:;<=>?@[\\\\\]^_`{|}~])/.exec(source.slice(i));
    const entity = /^&(#x[\da-f]{1,6}|#\d{1,7}|[a-z][\da-z]*);/i.exec(source.slice(i));
    if (escape) {
      raw = escape[0];
      piece = escape[1];
    } else if (entity) {
      const name = entity[1];
      const code =
        name[0] === '#'
          ? Number.parseInt(name.slice(/^#x/i.test(name) ? 2 : 1), /^#x/i.test(name) ? 16 : 10)
          : null;
      const char =
        code === null
          ? decodeNamedCharacterReference(name)
          : code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff)
            ? String.fromCodePoint(code)
            : null;
      if (char !== null && char !== false) {
        raw = entity[0];
        piece = char;
      }
    } else if (raw === '\r') {
      raw = source[i + 1] === '\n' ? '\r\n' : '\r';
      piece = '\n';
    }
    decoded += piece;
    i += raw.length;
    map.set(i, decoded.length);
  }
  return decoded === value ? map : null; // Ambiguous Markdown transformation: leave it unmarked.
}

export default function highlightPlugin(options: { source: string; spans: HighlightSpan[] }) {
  return (tree: unknown) => {
    function walk(node: TreeNode) {
      if (!node.children || node.tagName === 'code' || node.tagName === 'pre') return;
      const children: TreeNode[] = [];
      for (const child of node.children) {
        if (child.type !== 'text' || child.value === undefined) {
          walk(child);
          children.push(child);
          continue;
        }
        const start = child.position?.start.offset;
        const end = child.position?.end.offset;
        if (start === undefined || end === undefined) {
          children.push(child);
          continue;
        }
        const ranges = options.spans.filter((span) => span.start >= start && span.end <= end);
        const map = ranges.length
          ? decodedBoundaries(options.source.slice(start, end), child.value)
          : null;
        if (!map) {
          children.push(child);
          continue;
        }
        let cursor = 0;
        for (const range of ranges) {
          const from = map.get(range.start - start);
          const to = map.get(range.end - start);
          if (from === undefined || to === undefined || from < cursor || to <= from) continue;
          if (from > cursor)
            children.push({ type: 'text', value: child.value.slice(cursor, from) });
          children.push({
            type: 'element',
            tagName: 'mark',
            properties: { 'data-diff-id': range.id },
            children: [{ type: 'text', value: child.value.slice(from, to) }],
          });
          cursor = to;
        }
        if (cursor < child.value.length)
          children.push({ type: 'text', value: child.value.slice(cursor) });
      }
      node.children = children;
    }
    walk(tree as TreeNode);
  };
}
