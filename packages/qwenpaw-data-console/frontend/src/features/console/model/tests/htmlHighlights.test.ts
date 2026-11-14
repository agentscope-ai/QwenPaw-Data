import assert from 'node:assert/strict';
import test from 'node:test';
import { highlightComparedHtml } from '../compare/htmlHighlights.js';

test('HTML highlights use original code-point offsets, preserve entities and repeated values', () => {
  const html = '<p>😀 50</p>\r\n<p>费用 &#53;&#48;</p><script>50</script>';
  const normalized = html.replace(/\r\n/g, '\n');
  const start = Array.from(normalized.slice(0, normalized.indexOf('&#53;'))).length;
  const result = highlightComparedHtml(html, [{ id: 'diff-0', start, end: start + 10 }]);
  assert.match(result, /data-qwenpaw-data-diff-id="diff-0"[^>]*>&#53;&#48;<\/mark>/);
  assert.ok(result.includes('<p>😀 50</p>\r\n'));
  assert.ok(result.includes('<script>50</script>'));
});

test('HTML highlights reject invalid identities, overlapping or markup spans', () => {
  const html = '<p>123</p>';
  assert.equal(highlightComparedHtml(html, [{ id: 'diff-0', start: 0, end: 6 }]), html);
  assert.equal(highlightComparedHtml(html, [{ id: '" onclick="bad', start: 3, end: 6 }]), html);
  assert.equal(highlightComparedHtml(html, [{ id: 'diff-0', start: 99, end: 100 }]), html);
});
