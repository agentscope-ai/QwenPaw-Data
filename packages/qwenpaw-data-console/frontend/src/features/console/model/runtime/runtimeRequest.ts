import type { AttachmentRef } from '../../api/types';
import { parseAttachmentReferenceUrl } from '../chat/attachments.js';

function messageContent(item: unknown): unknown[] | null {
  if (!item || typeof item !== 'object' || !('content' in item)) return null;
  const content = (item as { content?: unknown }).content;
  return Array.isArray(content) ? content : null;
}

function contentText(content: readonly unknown[]): string {
  return content
    .flatMap((part) =>
      part &&
      typeof part === 'object' &&
      'type' in part &&
      part.type === 'text' &&
      'text' in part &&
      typeof part.text === 'string'
        ? [part.text]
        : [],
    )
    .join('\n')
    .trim();
}

function latestRuntimeUserContent(input: readonly unknown[]): unknown[] | null {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (!item || typeof item !== 'object' || !('role' in item) || item.role !== 'user') {
      continue;
    }
    return messageContent(item);
  }
  return null;
}

export function attachmentRefFromRuntimeContent(part: unknown): AttachmentRef | null {
  if (!part || typeof part !== 'object') return null;
  const value = part as {
    type?: unknown;
    file_id?: unknown;
    file_name?: unknown;
    fileName?: unknown;
    file_url?: unknown;
    image_url?: unknown;
    audio_url?: unknown;
    video_url?: unknown;
    data?: unknown;
  };
  const urls = [value.file_url, value.image_url, value.audio_url, value.video_url, value.data];
  for (const url of urls) {
    const attachment = parseAttachmentReferenceUrl(url);
    if (attachment) return attachment;
  }
  const filename =
    typeof value.file_name === 'string'
      ? value.file_name
      : typeof value.fileName === 'string'
        ? value.fileName
        : '';
  if (value.type === 'file' && typeof value.file_id === 'string' && filename) {
    return { attachment_id: value.file_id, filename };
  }
  return null;
}

export function extractLatestRuntimeText(input: readonly unknown[]): string {
  const content = latestRuntimeUserContent(input);
  return content ? contentText(content) : '';
}

export function extractLatestRuntimeAttachments(input: readonly unknown[]): AttachmentRef[] {
  const content = latestRuntimeUserContent(input);
  if (!content) return [];
  const seen = new Set<string>();
  return content.flatMap((part) => {
    const attachment = attachmentRefFromRuntimeContent(part);
    if (!attachment || seen.has(attachment.attachment_id)) return [];
    seen.add(attachment.attachment_id);
    return [attachment];
  });
}

export function shouldClearAcceptedRuntimeInput(
  currentValue: string,
  submittedText: string,
): boolean {
  return Boolean(submittedText) && currentValue.trim() === submittedText.trim();
}

export function shouldClearAcceptedRuntimeAttachments(
  currentAttachmentCount: number,
  submittedAttachmentCount: number,
): boolean {
  return submittedAttachmentCount > 0 && currentAttachmentCount === submittedAttachmentCount;
}

export function serializeRuntimeAttachmentIds(attachmentIds: readonly string[]): {
  attachment_ids?: string[];
} {
  return attachmentIds.length > 0 ? { attachment_ids: [...attachmentIds] } : {};
}
