import type { ArtifactAccessPurpose, ArtifactShare } from '../../api/types';
import type { Artifact } from '../../types';
import { getArtifactPreviewKind } from './artifactPreview.js';

export interface ArtifactApi {
  getWorkspaceFile: (
    sessionId: string,
    relativePath: string,
    purpose: ArtifactAccessPurpose,
  ) => Promise<Response>;
}

export interface ArtifactShareApi {
  shareWorkspaceFile: (sessionId: string, relativePath: string) => Promise<ArtifactShare>;
}

interface ArtifactAccessEnvironment {
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
  open: (url: string) => void;
  download: (url: string, filename: string) => void;
  scheduleRelease: (release: () => void) => void;
}

export interface ResolvedArtifactAccess {
  url: string;
  /** Original HTML for comment source ranges; never the instrumented display copy. */
  sourceText?: string;
  release: () => void;
}

const browserEnvironment: ArtifactAccessEnvironment = {
  createObjectURL: (blob) => URL.createObjectURL(blob),
  revokeObjectURL: (url) => URL.revokeObjectURL(url),
  open: (url) => {
    window.open(url, '_blank', 'noopener,noreferrer');
  },
  download: (url, filename) => {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = 'noopener noreferrer';
    anchor.click();
  },
  scheduleRelease: (release) => {
    window.setTimeout(release, 1000);
  },
};

export async function resolveArtifactAccess(
  sessionId: string,
  artifact: Artifact,
  purpose: ArtifactAccessPurpose,
  api: ArtifactApi,
  environment: Pick<
    ArtifactAccessEnvironment,
    'createObjectURL' | 'revokeObjectURL'
  > = browserEnvironment,
): Promise<ResolvedArtifactAccess> {
  if (!artifact.access) {
    throw new Error('该产物没有可用的文件访问标识');
  }

  const response = await api.getWorkspaceFile(sessionId, artifact.access.relativePath, purpose);
  if (purpose === 'preview' && getArtifactPreviewKind(artifact) === 'html') {
    // A network document uses the file endpoint's sandbox CSP. Both srcdoc
    // and blob URLs inherit the console CSP and block report scripts.
    if (!response.url) throw new Error('HTML preview response has no document URL');
    const sourceText = await response.text();
    return { url: response.url, sourceText, release: () => undefined };
  }
  const blob = await response.blob();
  const url = environment.createObjectURL(blob);
  return {
    url,
    release: () => environment.revokeObjectURL(url),
  };
}

export async function accessArtifact(
  sessionId: string,
  artifact: Artifact,
  purpose: ArtifactAccessPurpose,
  api: ArtifactApi,
  environment: ArtifactAccessEnvironment = browserEnvironment,
): Promise<void> {
  const access = await resolveArtifactAccess(sessionId, artifact, purpose, api, environment);
  try {
    if (purpose === 'preview') {
      environment.open(access.url);
    } else {
      environment.download(access.url, artifact.name);
    }
  } finally {
    environment.scheduleRelease(access.release);
  }
}

export async function shareArtifact(
  sessionId: string,
  artifact: Artifact,
  api: ArtifactShareApi,
): Promise<ArtifactShare> {
  const relativePath = artifact.access?.relativePath.trim();
  if (!relativePath) {
    throw new Error('该产物没有可用的文件访问标识');
  }

  return api.shareWorkspaceFile(sessionId, relativePath);
}
