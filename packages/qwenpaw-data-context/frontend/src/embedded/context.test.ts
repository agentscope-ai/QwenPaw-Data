import assert from 'node:assert/strict';
import test from 'node:test';
import { configureEmbedding, resolveCmUrl } from './context.ts';

test('standalone URLs are preserved; embedding separates CM business, auth, and file traffic', () => {
  assert.equal(
    resolveCmUrl('/api/semantic-config/dataset-meta'),
    '/api/semantic-config/dataset-meta',
  );
  configureEmbedding();
  for (const path of ['/api/semantic-config/dataset-meta', '/api/v1/graph/stream', '/api/kg/docs'])
    assert.equal(resolveCmUrl(path), path.replace('/api/', '/cm-api/'));
  assert.equal(resolveCmUrl('/web/reports/a.html'), '/cm-web/reports/a.html');
  for (const path of ['/api/auth/me', '/api/platform/tenants', '/api/users', '/api/workspaces'])
    assert.equal(resolveCmUrl(path), path);
  assert.equal(
    resolveCmUrl('/cm-api/semantic-config/dataset-meta'),
    '/cm-api/semantic-config/dataset-meta',
  );
});
