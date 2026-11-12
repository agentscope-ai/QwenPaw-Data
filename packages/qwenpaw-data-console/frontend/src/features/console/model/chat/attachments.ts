import type { AttachmentRef } from '../../api/types';

const ATTACHMENT_REFERENCE_PROTOCOL = 'qwenpaw-data-attachment:';
const ATTACHMENT_REFERENCE_HOST = 'attachment';

export const ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;

export const ATTACHMENT_EXTENSIONS = new Set([
  'csv',
  'xlsx',
  'xls',
  'json',
  'parquet',
  'txt',
  'md',
  'pdf',
  'png',
  'jpg',
  'jpeg',
  'zip',
  'sql',
  'gz',
  'html',
]);

export function attachmentExtension(filename: string): string {
  const dot = filename.lastIndexOf('.');
  if (dot <= 0 || dot === filename.length - 1) return '';
  return filename.slice(dot + 1).toLowerCase();
}

export function validateAttachmentFile(
  file: File,
  existingNames: readonly string[],
): 'empty' | 'tooLarge' | 'type' | 'duplicate' | null {
  if (!file.size) return 'empty';
  if (file.size > ATTACHMENT_MAX_BYTES) return 'tooLarge';
  if (!ATTACHMENT_EXTENSIONS.has(attachmentExtension(file.name))) return 'type';
  if (existingNames.includes(file.name)) return 'duplicate';
  return null;
}

export function createAttachmentReferenceUrl(attachment: AttachmentRef): string {
  const url = new URL(`${ATTACHMENT_REFERENCE_PROTOCOL}//${ATTACHMENT_REFERENCE_HOST}`);
  url.searchParams.set('id', attachment.attachment_id);
  url.searchParams.set('filename', attachment.filename);
  return url.toString();
}

export function parseAttachmentReferenceUrl(value: unknown): AttachmentRef | null {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value);
    if (
      url.protocol !== ATTACHMENT_REFERENCE_PROTOCOL ||
      url.hostname !== ATTACHMENT_REFERENCE_HOST
    ) {
      return null;
    }
    const attachmentId = url.searchParams.get('id')?.trim() ?? '';
    const filename = url.searchParams.get('filename')?.trim() ?? '';
    if (!attachmentId || !filename) return null;
    return { attachment_id: attachmentId, filename };
  } catch {
    return null;
  }
}
