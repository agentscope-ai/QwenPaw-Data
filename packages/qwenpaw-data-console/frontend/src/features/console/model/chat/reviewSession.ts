import type { Artifact, AttachmentRef, ConsoleChat, ConsoleSession } from '../../api/types.js';
import { createAttachmentReferenceUrl } from './attachments.js';

export function restoredAttachmentFiles(attachments: readonly AttachmentRef[]) {
  return attachments.map((attachment) => ({
    uid: attachment.attachment_id,
    name: attachment.filename,
    // SDK 1.1 calls type.indexOf while building requests, even for ordinary files.
    type: 'application/octet-stream',
    status: 'done' as const,
    file_id: attachment.attachment_id,
    response: { ...attachment, url: createAttachmentReferenceUrl(attachment) },
  }));
}

export function restoredAttachmentIds(value: string | undefined): string[] {
  if (!value) return [];
  try {
    const ids: unknown = JSON.parse(value);
    return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : [];
  } catch {
    return [];
  }
}

export function selectReviewReport(
  session: ConsoleSession | null,
  chats: readonly ConsoleChat[],
): Artifact | null {
  if (
    session?.agent_id !== 'review' ||
    !session.parent_session_id ||
    session.status === 'running' ||
    chats.some((chat) => chat.status === 'created' || chat.status === 'running')
  ) {
    return null;
  }
  const reports = chats
    .filter((chat) => chat.status === 'completed')
    .flatMap((chat) => chat.artifacts)
    .filter(
      (artifact) =>
        artifact.session_id === session.id && artifact.path.split('/').pop() === 'review-report.md',
    );
  return reports.sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0] ?? null;
}

export function appendReviewPrompt(draft: string, prompt: string): string {
  return draft.trim() ? `${draft}\n\n${prompt}` : prompt;
}

export function reviewReportFilename(uniqueId: string): string {
  return `review-report-${uniqueId}.md`;
}
