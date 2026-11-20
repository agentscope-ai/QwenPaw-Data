import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);

// The SDK still consumes query-string 7. Its patched CommonJS import must use
// the default export of the security-fixed ESM decoder, including in Node 22.
test('SDK query parser keeps Unicode, arrays and malformed escape handling', () => {
  const query = require('query-string');
  assert.deepEqual(
    { ...query.parse('q=%E4%B8%AD%E6%96%87+hello&item=a&item=b') },
    { q: '中文 hello', item: ['a', 'b'] },
  );
  assert.equal(query.parse('q=%E0%A4%A').q, '%E0%A4%A');
  assert.equal(query.parse('q=%').q, '%');
});

test('SDK query serializer preserves its existing request format', () => {
  const query = require('query-string');
  assert.equal(
    query.stringify({ q: '中文 hello', item: ['a', 'b'] }),
    'item=a&item=b&q=%E4%B8%AD%E6%96%87%20hello',
  );
  assert.deepEqual(
    { ...query.parseUrl('https://example.invalid/?x=1%202&x=3').query },
    { x: ['1 2', '3'] },
  );
});

test('SDK syntax tree keeps JavaScript and SQL grammars with patched Prism', () => {
  const refractor = require('refractor');
  for (const [language, code] of [
    ['javascript', 'const answer = 42;'],
    ['sql', 'SELECT count(*) FROM events;'],
  ]) {
    const tree = refractor.highlight(code, language);
    assert.ok(tree.length > 0);
    assert.match(JSON.stringify(tree), /keyword/);
  }
});
