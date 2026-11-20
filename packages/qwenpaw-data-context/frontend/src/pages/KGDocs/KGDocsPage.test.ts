/** Static wiring checks: node --experimental-strip-types src/pages/KGDocs/KGDocsPage.test.ts */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const page = readFileSync(new URL('./KGDocsPage.tsx', import.meta.url), 'utf8');

test('retry dispatches deletion and reingestion to their existing handlers', () => {
  assert.match(
    page,
    /if\s*\(record\.retry_action === (['"])delete\1\)\s*\{\s*handleDelete\(record\);\s*\}\s*else\s*\{\s*void handleReingest\(record\);\s*\}/,
  );
});

test('the delete error handler refreshes the document list', () => {
  const deletion = page.slice(
    page.indexOf('const handleDelete ='),
    page.indexOf('const statusTabs ='),
  );
  assert.match(deletion, /catch\s*\(error\)\s*\{[^}]*await loadDocs\(\{\s*silent:\s*true\s*\}\)/);
});

test('deletion labels are wired and translated in every supported locale', () => {
  for (const key of ['retryDelete', 'status.deleteFailed', 'deleteFailToast']) {
    assert.ok(
      page.includes(`'kgDocs.${key}'`) || page.includes(`"kgDocs.${key}"`),
      'Missing page label: ' + key,
    );
  }
  for (const language of ['zh', 'en', 'ja', 'ru']) {
    const { kgDocs } = JSON.parse(
      readFileSync(new URL('../../i18n/locales/' + language + '.json', import.meta.url), 'utf8'),
    );
    for (const text of [kgDocs.retryDelete, kgDocs.status.deleteFailed, kgDocs.deleteFailToast]) {
      assert.ok(
        typeof text === 'string' && text.length > 0,
        language + ': missing KG deletion text',
      );
    }
  }
});
