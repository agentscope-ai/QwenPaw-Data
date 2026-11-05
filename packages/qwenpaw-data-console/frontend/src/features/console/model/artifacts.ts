import { getApiUrl } from '@/services/config';
import type { ArtifactAccessPurpose } from '../api/types';
import type { Artifact } from '../types';

export function workspacePreviewUrl(sessionId: string, relativePath: string) {
  const search = new URLSearchParams({
    path: relativePath,
    purpose: 'preview',
  });
  return getApiUrl(`/sessions/${sessionId}/files/access?${search.toString()}`);
}

export interface ArtifactApi {
  getWorkspaceFile: (
    sessionId: string,
    relativePath: string,
    purpose: ArtifactAccessPurpose,
  ) => Promise<Response>;
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
  sizeBytes: number;
  release: () => void;
}

const browserEnvironment: ArtifactAccessEnvironment = {
  createObjectURL: (blob) => URL.createObjectURL(blob),
  revokeObjectURL: (url) => URL.revokeObjectURL(url),
  open: (url) => {
    window.open(url, '_blank', 'noopener');
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
  environment: Pick<ArtifactAccessEnvironment, 'createObjectURL' | 'revokeObjectURL'> =
    browserEnvironment,
  options?: { maxBytes?: number },
): Promise<ResolvedArtifactAccess> {
  if (!artifact.access) {
    throw new Error('该产物没有可用的文件访问标识');
  }

  const response = await api.getWorkspaceFile(
    sessionId,
    artifact.access.relativePath,
    purpose,
  );
  const headerSize = Number(response.headers.get('content-length')) || 0;
  if (options?.maxBytes !== undefined && headerSize > options.maxBytes) {
    await response.body?.cancel().catch(() => undefined);
    return {
      url: '',
      sizeBytes: headerSize,
      release: () => undefined,
    };
  }
  const blob = await response.blob();
  if (options?.maxBytes !== undefined && blob.size > options.maxBytes) {
    return {
      url: '',
      sizeBytes: blob.size,
      release: () => undefined,
    };
  }
  const url = environment.createObjectURL(blob);
  return {
    url,
    sizeBytes: blob.size || headerSize,
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
  if (
    purpose === 'preview'
    && artifact.access?.relativePath
    && /\.html?$/i.test(artifact.name)
  ) {
    environment.open(workspacePreviewUrl(sessionId, artifact.access.relativePath));
    return;
  }
  const access = await resolveArtifactAccess(
    sessionId,
    artifact,
    purpose,
    api,
    environment,
  );
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
