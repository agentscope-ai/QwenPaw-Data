import type { ArtifactComment } from '../../api/types';
import type { Artifact } from '../../types';
import {
  getArtifactPreviewKind,
  isCommentablePreviewKind,
  MAX_INLINE_TEXT_BYTES,
} from './artifactPreview.js';

export interface ArtifactCommentHighlight {
  lineStart: number;
  lineEnd: number;
  comment?: string;
}

export type PendingArtifactComment = ArtifactComment;

export function artifactFilePath(artifact: Artifact): string {
  return artifact.path || artifact.access?.relativePath || '';
}

export function isArtifactCommentable(artifact: Artifact): boolean {
  return (
    Boolean(artifactFilePath(artifact)) &&
    isCommentablePreviewKind(getArtifactPreviewKind(artifact)) &&
    (artifact.sizeBytes === undefined || artifact.sizeBytes <= MAX_INLINE_TEXT_BYTES)
  );
}

export function isArtifactSplitCommentPreview(artifact: Artifact, commentMode: boolean): boolean {
  const kind = getArtifactPreviewKind(artifact);
  return commentMode && isArtifactCommentable(artifact) && (kind === 'html' || kind === 'markdown');
}

export function artifactFromCommentPath(path: string): Artifact {
  const name = path.split('/').pop() || path;
  const extension = name.includes('.') ? name.split('.').pop()?.toUpperCase() || 'FILE' : 'FILE';
  return {
    id: path,
    name,
    title: name,
    type: extension,
    meta: path,
    path,
    access: { relativePath: path },
  };
}

export function toArtifactComment(item: PendingArtifactComment): ArtifactComment {
  return {
    path: item.path,
    line_start: item.line_start,
    line_end: item.line_end,
    comment: item.comment,
  };
}

export function artifactCommentKey(item: ArtifactComment): string {
  return [item.path, item.line_start, item.line_end, item.comment].join(':');
}

export function artifactCommentRange(item: ArtifactComment): string {
  return item.line_start === item.line_end
    ? `L${item.line_start}`
    : `L${item.line_start}\u2013L${item.line_end}`;
}

export function commentsFromMessageMetadata(
  metadata: Record<string, unknown> | null | undefined,
): ArtifactComment[] {
  const raw = metadata?.artifact_comments;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is ArtifactComment => {
    if (!item || typeof item !== 'object') return false;
    const comment = item as Record<string, unknown>;
    return (
      typeof comment.path === 'string' &&
      typeof comment.line_start === 'number' &&
      typeof comment.line_end === 'number' &&
      typeof comment.comment === 'string'
    );
  });
}

function artifactCommentHeadingPattern(): RegExp {
  return /^\u5173\u4e8e\s+`([^`]+)`\s+(L\d+(?:\u2013L\d+)?)\uff1a\s*/gm;
}

export function visibleArtifactCommentText(text: string): string {
  return text
    .replace(artifactCommentHeadingPattern(), '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}
