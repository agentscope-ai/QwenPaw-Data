import assert from 'node:assert/strict';
import test from 'node:test';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkRehype from 'remark-rehype';
import rehypeKatex from 'rehype-katex';
import { mapHtmlBlocks, mapMarkdownBlocks } from '../artifacts/commentSourceMap.js';
import { createCommentHtml } from '../artifacts/commentPreviewBridge.js';
import {
  normalizeCommentMath,
  remarkCommentCompatibility,
} from '../artifacts/commentMarkdownCompatibility.js';

const attribute = 'data-fd-comment-test';

test('HTML marker insertion preserves exact original bytes, line numbers and tag syntax', () => {
  for (const source of [
    '',
    '<p>Hello</p>',
    '<!doctype html>\r\n<html><head></head><body>\r\n<p class=x>A &amp; B</p>\r\n<p>A &amp; B</p>\r\n</body></html>\r\n',
    '<img src=x/>',
    '<p>a\rb</p>',
  ]) {
    const mapped = mapHtmlBlocks(source, attribute);
    assert.equal(mapped.html.replace(/ data-fd-comment-test="\d+"/g, ''), source);
    for (const block of mapped.blocks)
      for (const range of block.ranges) {
        assert.ok(range.start >= 1 && range.end <= source.split('\n').length);
      }
  }
  const mapped = mapHtmlBlocks('<p>same</p>\r\n<p>same</p>', attribute);
  assert.deepEqual(
    mapped.blocks.map((b) => [...new Set(b.ranges.map((r) => r.start))]),
    [[1], [2]],
  );
  assert.notEqual(mapped.blocks[0].id, mapped.blocks[1].id);
});

test('HTML script, style, comments, blank lines and nested iframe content do not map to unrelated ancestors', () => {
  const source =
    '<main>\n<p>first</p>\n\n<!-- comment -->\n<script>const x=1</script>\n<style>p{color:red}</style>\n<iframe srcdoc="x"></iframe>\n<p>last</p>\n</main>';
  const { blocks } = mapHtmlBlocks(source, attribute);
  const lines = new Set(blocks.flatMap((block) => block.ranges.map((r) => r.start)));
  for (const line of [3, 4, 5, 6, 7]) assert.equal(lines.has(line), false);
  assert.ok(lines.has(1) && lines.has(2) && lines.has(8) && lines.has(9));
});

test('HTML list label markers preserve original bytes, inline formatting and source lines', () => {
  const source =
    '<ul>\r\n<li class=parent>parent &amp; <strong>bold</strong> tail\r\n<ul><li>child</li></ul>\r\n</li>\r\n<li>last</li>\r\n</ul>';
  const mapped = mapHtmlBlocks(source, attribute);
  assert.equal(
    mapped.html
      .replace(/<span data-fd-comment-test="\d+">([\s\S]*?)<\/span>/g, '$1')
      .replace(/ data-fd-comment-test="\d+"/g, ''),
    source,
  );
  const labels = [
    ...mapped.html.matchAll(/<span data-fd-comment-test="(\d+)">([\s\S]*?)<\/span>/g),
  ];
  assert.deepEqual(
    labels.map((match) => match[2]),
    ['parent &amp; ', ' tail\r\n'],
  );
  for (const label of labels)
    assert.deepEqual(mapped.blocks.find((block) => block.id === label[1])?.ranges, [
      { start: 2, end: 2 },
    ]);
  assert.match(mapped.html, /<strong data-fd-comment-test="\d+">bold<\/strong>/);
  assert.match(mapped.html, /<ul data-fd-comment-test="\d+"><li data-fd-comment-test="\d+">child/);
});

test('Markdown uses original AST positions for repeated text, table cells, code, math and CRLF', async () => {
  const source = [
    '# Report',
    '',
    'same &amp; text',
    '',
    'same &amp; text',
    '',
    '| a | b |',
    '| - | - |',
    '| 1 | 2 |',
    '',
    '```mermaid',
    'graph TD; A-->B',
    '```',
    '',
    '$$',
    'x^2',
    '$$',
    '',
  ].join('\r\n');
  const processor = unified().use(remarkParse).use(remarkGfm).use(remarkMath).use(remarkRehype);
  const tree = await processor.run(processor.parse(source));
  const blocks = mapMarkdownBlocks(tree, attribute, source);
  const ranges = blocks.flatMap((b) => b.ranges);
  for (const line of [1, 3, 5, 7, 9])
    assert.ok(ranges.some((r) => r.start === line && r.end === line));
  assert.ok(ranges.some((r) => r.start === 11 && r.end === 13));
  assert.ok(ranges.some((r) => r.start === 15 && r.end === 17));
  assert.equal(
    ranges.some((r) => r.start === 2),
    false,
  );
  const before = JSON.stringify(tree);
  assert.match(before, /language-mermaid/);
  // KaTeX must preserve the outer source marker while generating its internal DOM.
  const rendered = await unified().use(rehypeKatex).run(tree);
  const encoded = JSON.stringify(rendered);
  assert.match(encoded, /katex/);
  for (const block of blocks) assert.ok(encoded.includes(`"${attribute}":"${block.id}"`));
});

test('HTML bootstrap is inserted before report scripts without modifying original source or embedding its text in JS', () => {
  const source =
    '<!doctype html><html><head><script>window.report=1</script></head><body><p>target</p></body></html>';
  const html = createCommentHtml(source, 'test');
  assert.ok(html.startsWith('<!doctype html><html><head><script>'));
  assert.ok(html.indexOf('installBridge') < html.indexOf('window.report=1'));
  assert.ok(html.includes('<p data-fd-comment-test="0">target</p>'));
  assert.throws(() => createCommentHtml(source, '</script>'));
  assert.throws(() => mapHtmlBlocks(source, 'x" onclick="bad'));
});

test('SDK math delimiters and br remain supported without changing offsets or code content', async () => {
  const source = [
    'Text \\(x^2\\) and<br>next',
    '',
    '\\[',
    '\\begin{align*}x&=1\\end{align*}',
    '\\]',
    '',
    '$',
    'x+1',
    '$',
    '',
    '`\\(literal\\)`',
    '',
    '```text',
    '\\[not math\\]',
    '```',
  ].join('\r\n');
  const rawParser = unified().use(remarkParse).use(remarkMath);
  const normalized = normalizeCommentMath(source, rawParser.parse(source));
  assert.equal(normalized.length, source.length);
  assert.deepEqual(
    [...normalized.matchAll(/\n/g)].map((m) => m.index),
    [...source.matchAll(/\n/g)].map((m) => m.index),
  );
  assert.ok(normalized.includes('`\\(literal\\)`'));
  assert.ok(normalized.includes('\\[not math\\]'));
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkMath)
    .use(remarkCommentCompatibility)
    .use(remarkRehype);
  const tree = await processor.run(processor.parse(source), { value: source });
  const blocks = mapMarkdownBlocks(tree, attribute, source);
  assert.ok(blocks.some((b) => b.ranges.some((r) => r.start === 3 && r.end === 5)));
  assert.ok(blocks.some((b) => b.ranges.some((r) => r.start === 7 && r.end === 9)));
  const rendered = await unified().use(rehypeKatex).run(tree);
  const encoded = JSON.stringify(rendered);
  assert.match(encoded, /"tagName":"br"/);
  assert.match(encoded, /katex/);
  assert.doesNotMatch(encoded, /katex-error/);
  for (const block of blocks) assert.ok(encoded.includes(`"${attribute}":"${block.id}"`));
});
