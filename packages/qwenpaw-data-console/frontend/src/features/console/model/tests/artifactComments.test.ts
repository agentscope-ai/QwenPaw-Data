import assert from 'node:assert/strict';
import test from 'node:test';
import {
  artifactFromCommentPath,
  commentsFromMessageMetadata,
  isArtifactCommentable,
  isArtifactSplitCommentPreview,
  toArtifactComment,
} from '../artifacts/artifactComments.js';
import { MAX_INLINE_TEXT_BYTES } from '../artifacts/artifactPreview.js';

test('HTML extension, alias and MIME enable source comments and split rendering', () => {
  const artifacts = [
    artifactFromCommentPath('reports/result.html'),
    artifactFromCommentPath('reports/result.htm'),
    { ...artifactFromCommentPath('reports/result'), mimeType: 'text/html' },
  ];
  for (const artifact of artifacts) {
    assert.equal(isArtifactCommentable(artifact), true);
    assert.equal(isArtifactSplitCommentPreview(artifact, true), true);
    assert.equal(isArtifactSplitCommentPreview(artifact, false), false);
  }
});

test('split comments preserve path and size gates, including empty and unknown sizes', () => {
  for (const extension of ['html', 'md']) {
    const artifact = artifactFromCommentPath(`reports/result.${extension}`);
    for (const sizeBytes of [undefined, 0, MAX_INLINE_TEXT_BYTES]) {
      assert.equal(isArtifactCommentable({ ...artifact, sizeBytes }), true);
      assert.equal(isArtifactSplitCommentPreview({ ...artifact, sizeBytes }, true), true);
    }
    for (const blocked of [
      { ...artifact, sizeBytes: MAX_INLINE_TEXT_BYTES + 1 },
      { ...artifact, path: undefined, access: undefined },
    ]) {
      assert.equal(isArtifactCommentable(blocked), false);
      assert.equal(isArtifactSplitCommentPreview(blocked, true), false);
    }
    assert.equal(isArtifactCommentable({ ...artifact, path: undefined }), true);
    assert.equal(isArtifactCommentable({ ...artifact, access: undefined }), true);
  }
});

test('other commentable formats stay source-only and binary formats remain excluded', () => {
  for (const extension of ['csv', 'json', 'txt']) {
    const artifact = artifactFromCommentPath(`reports/result.${extension}`);
    assert.equal(isArtifactCommentable(artifact), true);
    assert.equal(isArtifactSplitCommentPreview(artifact, true), false);
  }
  for (const extension of ['pdf', 'png', 'mp4', 'mp3']) {
    const artifact = artifactFromCommentPath(`reports/result.${extension}`);
    assert.equal(isArtifactCommentable(artifact), false);
    assert.equal(isArtifactSplitCommentPreview(artifact, true), false);
  }
});

test('HTML comments retain the four-field contract and can reopen from message metadata', () => {
  const comment = {
    path: 'reports/result.html',
    line_start: 4,
    line_end: 6,
    comment: 'Update the report title',
  };
  const serialized = JSON.stringify(toArtifactComment(comment));
  assert.equal(serialized, JSON.stringify(comment));
  const restored = commentsFromMessageMetadata({
    artifact_comments: JSON.parse(`[${serialized}]`),
  });
  assert.deepEqual(restored, [comment]);
  const artifact = artifactFromCommentPath(restored[0].path);
  assert.equal(artifact.access?.relativePath, comment.path);
  assert.equal(isArtifactSplitCommentPreview(artifact, true), true);
});
