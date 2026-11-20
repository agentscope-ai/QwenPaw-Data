import assert from 'node:assert/strict';
import test from 'node:test';
import { isMissingArtifact, parseArtifactJson } from '../report/artifactContent.js';

test('only a missing workspace file is treated as not generated', () => {
  const missing = { status: 404, code: 'NOT_FOUND', message: 'workspace file not found' };
  assert.equal(isMissingArtifact(missing), true);
  assert.equal(isMissingArtifact({ ...missing, message: 'session not found' }), false);
  assert.equal(isMissingArtifact({ ...missing, code: null }), false);
  assert.equal(isMissingArtifact({ ...missing, status: 403 }), false);
  assert.equal(isMissingArtifact({ ...missing, status: 500 }), false);
});

test('missing JSON is nullable but malformed or empty JSON remains an error with its path', () => {
  const path = 'report-reproduction/report_manifest.json';
  assert.equal(parseArtifactJson(null, path), null);
  assert.deepEqual(parseArtifactJson('{"sections":[]}', path), { sections: [] });
  for (const text of ['', '{broken']) {
    assert.throws(
      () => parseArtifactJson(text, path),
      (error: unknown) =>
        error instanceof Error &&
        error.message.includes(path) &&
        'cause' in error &&
        error.cause instanceof SyntaxError,
    );
  }
});
