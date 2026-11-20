import assert from 'node:assert/strict';
import test from 'node:test';
import { codePointBoundaries, splitTextSpans, utf16Span } from '../compare/textSpans.js';

test('backend spans keep the second repeated value after an emoji', () => {
  const parts = splitTextSpans('😀 100 100', [{ start: 6, end: 9 }]);
  assert.deepEqual(parts, [
    { text: '😀 100 ', highlighted: false, start: 0 },
    { text: '100', highlighted: true, start: 7 },
  ]);
});

test('half-open ranges preserve every character and merge overlap without duplication', () => {
  const parts = splitTextSpans('abcde', [
    { start: 2, end: 4 },
    { start: 1, end: 3 },
  ]);
  assert.equal(parts.map((part) => part.text).join(''), 'abcde');
  assert.equal(
    parts
      .filter((part) => part.highlighted)
      .map((part) => part.text)
      .join(''),
    'bcd',
  );
  assert.equal(splitTextSpans('abc', [])[0].highlighted, false);
  assert.deepEqual(splitTextSpans('', []), []);
});

test('invalid spans are ignored instead of clamping to unrelated text', () => {
  const bounds = codePointBoundaries('😀1');
  for (const range of [
    { start: -1, end: 1 },
    { start: 0.5, end: 1 },
    { start: 1, end: 1 },
    { start: 0, end: 3 },
    { start: NaN, end: 1 },
  ]) {
    assert.equal(utf16Span(bounds, range), null);
  }
  assert.deepEqual(utf16Span(bounds, { start: 1, end: 2 }), { start: 2, end: 3 });
});
