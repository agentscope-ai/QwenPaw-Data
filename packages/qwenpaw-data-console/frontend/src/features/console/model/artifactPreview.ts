import type { Artifact } from '../types';

export type ArtifactPreviewKind =
  | 'audio'
  | 'csv'
  | 'html'
  | 'image'
  | 'json'
  | 'markdown'
  | 'pdf'
  | 'text'
  | 'unsupported'
  | 'video';

const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'jpeg', 'jpg', 'png', 'webp']);
const TEXT_EXTENSIONS = new Set([
  'css', 'log', 'py', 'sql', 'text', 'txt', 'ts', 'tsx', 'js', 'jsx', 'xml', 'yaml', 'yml',
]);
const AUDIO_EXTENSIONS = new Set(['aac', 'flac', 'm4a', 'mp3', 'ogg', 'wav']);
const VIDEO_EXTENSIONS = new Set(['m4v', 'mov', 'mp4', 'ogg', 'webm']);

function getExtension(fileName: string) {
  const extension = fileName.split('.').pop()?.toLowerCase();
  return extension && extension !== fileName.toLowerCase() ? extension : '';
}

export function getArtifactPreviewKind(artifact: Artifact): ArtifactPreviewKind {
  const extension = getExtension(artifact.name);
  const mimeType = artifact.mimeType?.split(';', 1)[0]?.trim().toLowerCase() ?? '';
  const artifactType = artifact.type.toLowerCase();

  if (mimeType === 'application/pdf' || extension === 'pdf') return 'pdf';
  if (
    mimeType === 'text/html'
    || artifactType === 'html'
    || extension === 'html'
    || extension === 'htm'
  ) {
    return 'html';
  }
  if (mimeType === 'text/csv' || artifactType === 'csv' || extension === 'csv' || extension === 'tsv') {
    return 'csv';
  }
  if (
    mimeType === 'application/json'
    || mimeType === 'application/geo+json'
    || extension === 'json'
    || extension === 'geojson'
  ) {
    return 'json';
  }
  if (mimeType === 'text/markdown' || artifactType === 'markdown' || extension === 'md' || extension === 'mdx') {
    return 'markdown';
  }
  if ((mimeType.startsWith('image/') && mimeType !== 'image/svg+xml') || IMAGE_EXTENSIONS.has(extension)) {
    return 'image';
  }
  if (mimeType.startsWith('audio/') || AUDIO_EXTENSIONS.has(extension)) return 'audio';
  if (mimeType.startsWith('video/') || VIDEO_EXTENSIONS.has(extension)) return 'video';
  if (mimeType.startsWith('text/') || TEXT_EXTENSIONS.has(extension) || ['python', 'sql'].includes(artifactType)) {
    return 'text';
  }
  return 'unsupported';
}

export interface CsvPreview {
  rows: string[][];
  truncated: boolean;
}

export function parseCsvPreview(
  source: string,
  delimiter = ',',
  maxRows = 200,
  maxColumns = 50,
): CsvPreview {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = '';
  let quoted = false;
  let index = 0;
  let truncated = false;

  const pushValue = () => {
    if (row.length < maxColumns) row.push(value);
    value = '';
  };
  const pushRow = () => {
    pushValue();
    rows.push(row);
    row = [];
  };

  while (index < source.length) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        value += '"';
        index += 2;
        continue;
      }
      if (char === '"') {
        quoted = false;
      } else {
        value += char;
      }
    } else if (char === '"' && value.length === 0) {
      quoted = true;
    } else if (char === delimiter) {
      pushValue();
    } else if (char === '\n') {
      pushRow();
      if (rows.length >= maxRows) {
        truncated = index + 1 < source.length;
        break;
      }
    } else if (char !== '\r') {
      value += char;
    }
    index += 1;
  }

  if (!truncated && (value.length > 0 || row.length > 0)) pushRow();
  return { rows, truncated };
}

export const MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024;
const MAX_JSON_PRETTY_CHARS = 200_000;

export function isInlinePreviewTooLarge(sizeBytes: number | undefined): boolean {
  return sizeBytes !== undefined && sizeBytes > MAX_INLINE_TEXT_BYTES;
}

export function formatJsonPreview(source: string) {
  if (source.length > MAX_JSON_PRETTY_CHARS) {
    return `${source.slice(0, MAX_JSON_PRETTY_CHARS)}\n\n…（内容过长，已截断，完整文件请下载）`;
  }
  try {
    return JSON.stringify(JSON.parse(source), null, 2);
  } catch {
    return source;
  }
}
