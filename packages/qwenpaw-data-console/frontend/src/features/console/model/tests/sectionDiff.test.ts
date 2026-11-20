import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import highlightPlugin, {
  decodedBoundaries,
} from '../../components/SectionComparePanel/highlightPlugin.js';
import {
  buildDiffIndex,
  projectDiffSpans,
  splitDraftDiffSection,
  shouldPollComparison,
} from '../report/sectionDiff.js';

function spans(text: string, values: Array<{ start?: number; end?: number; target: string }>) {
  const rows = buildDiffIndex({
    numeric_differences: values.map((v) => ({
      reference: '0',
      target: v.target,
      matched: false,
      target_start: v.start,
      target_end: v.end,
    })),
  }).rows;
  const split = splitDraftDiffSection(text);
  return { split, ranges: projectDiffSpans(text, rows, split.retained) };
}

function render(source: string, ranges: ReturnType<typeof spans>['ranges']) {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, {
      children: source,
      remarkPlugins: [remarkGfm, remarkMath],
      rehypePlugins: [[highlightPlugin, { source, spans: ranges }], rehypeKatex],
    }),
  );
}

test('emoji and duplicate values retain distinct ids without guessing missing offsets', () => {
  const source = '😀 100 and 100';
  const { ranges } = spans(source, [
    { start: 2, end: 5, target: '100' },
    { start: 10, end: 13, target: '100' },
    { target: '100' },
    { start: 2.5, end: 5, target: '100' },
    { start: -1, end: 2, target: '100' },
  ]);
  assert.deepEqual(ranges, [
    { id: 'diff-0', start: 3, end: 6 },
    { id: 'diff-1', start: 11, end: 14 },
  ]);
  const html = render(source, ranges);
  assert.match(html, /data-diff-id="diff-0">100/);
  assert.match(html, /data-diff-id="diff-1">100/);
});

test('middle narrative removal preserves CRLF and projects both retained fragments', () => {
  const source =
    '# Report\r\n😀 100\r\n\r\n## 复现差异与数据缺口说明\r\n- missing 200\r\n## Body\r\n300\r\n';
  const at = (v: string) =>
    Array.from(source.slice(0, source.indexOf(v)).replace(/\r\n?/g, '\n')).length;
  const { split, ranges } = spans(
    source,
    ['100', '200', '300'].map((target) => ({ start: at(target), end: at(target) + 3, target })),
  );
  assert.equal(split.body, '# Report\r\n😀 100\r\n\r\n## Body\r\n300\r\n');
  assert.equal(split.diff, '- missing 200');
  assert.deepEqual(
    ranges.map((r) => [r.id, split.body.slice(r.start, r.end)]),
    [
      ['diff-0', '100'],
      ['diff-2', '300'],
    ],
  );
});

test('only exact H2 outside a code fence removes the narrative', () => {
  for (const source of [
    '### 复现差异与数据缺口说明\n100',
    '## 复现差异与数据缺口说明附录\n100',
    '```md\n## 复现差异与数据缺口说明\n```\n100',
  ]) {
    assert.equal(splitDraftDiffSection(source).body, source);
  }
  const split = splitDraftDiffSection(
    '## 复现差异与数据缺口说明\n- gap\n### detail\nx\n## Body\n100',
  );
  assert.equal(split.body, '## Body\n100');
  assert.match(split.diff ?? '', /### detail/);
});

test('rendered Markdown preserves code and safely maps escapes and entities', () => {
  const source =
    '😀 &amp; \\* 100 `100`\n\n```text\n100\n```\n\n| Value |\n| --- |\n| 200 |\n\n$x^2$';
  const rows = [...source.matchAll(/100|200/g)].map((m) => ({
    start: Array.from(source.slice(0, m.index)).length,
    end: Array.from(source.slice(0, m.index)).length + 3,
    target: m[0],
  }));
  const { ranges } = spans(source, rows);
  const html = render(source, ranges);
  assert.equal((html.match(/data-diff-id=/g) ?? []).length, 2);
  assert.match(html, /<code>100<\/code>/);
  assert.match(html, /<pre><code class="language-text">100/);
  assert.match(html, /<table>/);
  assert.match(html, /class="katex"/);
  assert.equal(decodedBoundaries('&amp; \\* 100', '& * 100')?.get(9), 4);
  assert.equal(decodedBoundaries('> 100', '100'), null);
});

test('removed or cross-boundary spans never become false marks; baseline-only polling', () => {
  const text = '100\n## 复现差异与数据缺口说明\n200\n## End\n300';
  const { ranges } = spans(text, [{ start: 0, end: Array.from(text).length, target: text }]);
  assert.deepEqual(ranges, []);
  assert.equal(shouldPollComparison(true, false, 'validating'), true);
  for (const state of ['approved', 'failed', 'checking', 'pending', undefined])
    assert.equal(shouldPollComparison(true, false, state), false);
  assert.equal(shouldPollComparison(true, true, 'validating'), false);
  assert.equal(shouldPollComparison(false, false, 'validating'), false);
});
