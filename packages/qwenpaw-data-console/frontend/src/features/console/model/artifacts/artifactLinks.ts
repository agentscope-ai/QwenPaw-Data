import { artifactFromCommentPath } from './artifactComments.js';
import type { Artifact } from '../../types';

const ARTIFACT_LINK_PREFIX = '#artifact:';

export function artifactFromLink(href: string | null): Artifact | null {
  if (!href?.startsWith(ARTIFACT_LINK_PREFIX)) return null;

  let path: string;
  try {
    // Markdown URL-encodes filenames, including Chinese characters and spaces.
    path = decodeURIComponent(href.slice(ARTIFACT_LINK_PREFIX.length));
  } catch {
    return null;
  }
  return path.trim() ? artifactFromCommentPath(path) : null;
}
