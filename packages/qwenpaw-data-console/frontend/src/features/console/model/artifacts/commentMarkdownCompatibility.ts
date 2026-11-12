import type { Processor } from 'unified';

interface Node {
  type: string;
  value?: string;
  children?: Node[];
  position?: { start: { offset?: number }; end: { offset?: number } };
  data?: Record<string, unknown>;
}

/** Preserve UTF-16 offsets/newlines while accepting the SDK's alternate math delimiters. */
export function normalizeCommentMath(source: string, tree: unknown): string {
  if (!/\\[([]/.test(source)) return source;
  const protectedRanges: { start: number; end: number }[] = [];
  function collect(node: Node) {
    if (node.type === 'code' || node.type === 'inlineCode') {
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) protectedRanges.push({ start, end });
      return;
    }
    node.children?.forEach(collect);
  }
  collect(tree as Node);
  const output = source.split('');
  const pattern = /\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]/g;
  for (const match of source.matchAll(pattern)) {
    const start = match.index!;
    const end = start + match[0].length;
    let escapes = 0;
    for (let i = start - 1; i >= 0 && source[i] === '\\'; i--) escapes++;
    if (escapes % 2 || protectedRanges.some((range) => range.start < end && range.end > start))
      continue;
    const display = match[2] !== undefined;
    output[start] = '$';
    output[start + 1] = display ? '$' : ' ';
    output[end - 2] = display ? '$' : ' ';
    output[end - 1] = '$';
  }
  return output.join('');
}

export function remarkCommentCompatibility(this: Processor) {
  const parser = this.parser;
  if (parser)
    this.parser = (source, file) => {
      const tree = parser(source, file);
      const normalized = normalizeCommentMath(source, tree);
      return normalized === source ? tree : parser(normalized, file);
    };
  return (tree: unknown, file: { value: unknown }) => {
    const source = String(file.value);
    function visit(node: Node) {
      if (node.type === 'html' && /^<br\s*\/?>$/i.test(node.value?.trim() ?? '')) {
        node.type = 'break';
        delete node.value;
      }
      if (node.type === 'paragraph') {
        const start = node.position?.start.offset;
        const end = node.position?.end.offset;
        const singleDollar =
          start !== undefined && end !== undefined
            ? /^\$\r?\n([\s\S]+)\r?\n\$$/.exec(source.slice(start, end))
            : null;
        if (singleDollar) {
          node.type = 'math';
          node.value = singleDollar[1];
          delete node.children;
          node.data = {
            hName: 'pre',
            hChildren: [
              {
                type: 'element',
                tagName: 'code',
                properties: { className: ['language-math', 'math-display'] },
                children: [{ type: 'text', value: node.value }],
              },
            ],
          };
        }
      }
      if (node.type === 'math' || node.type === 'inlineMath') {
        const replace = (value: string) => value.replace(/\{align\*\}/g, '{aligned}');
        if (node.value) node.value = replace(node.value);
        function fixText(child: Node) {
          if (child.value) child.value = replace(child.value);
          child.children?.forEach(fixText);
        }
        if (Array.isArray(node.data?.hChildren)) (node.data.hChildren as Node[]).forEach(fixText);
      }
      node.children?.forEach(visit);
    }
    visit(tree as Node);
  };
}
