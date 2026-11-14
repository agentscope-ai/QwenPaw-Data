import type { SkillFileChange, SkillProposal } from '../../api/skillProposals';

export function getSkillProposalFileChanges(proposal: SkillProposal): SkillFileChange[] {
  if (proposal.file_changes?.length) return proposal.file_changes;
  return proposal.patches ?? proposal.file_changes ?? [];
}

export function collectSkillHunkIds(files: SkillFileChange[]): string[] {
  return files.flatMap((file) => (file.hunks ?? []).map((hunk) => hunk.hunk_id).filter(Boolean));
}

export function countSelectedSkillHunks(
  file: SkillFileChange,
  selectedHunkIds: ReadonlySet<string>,
): number {
  return (file.hunks ?? []).reduce(
    (count, hunk) => count + (selectedHunkIds.has(hunk.hunk_id) ? 1 : 0),
    0,
  );
}
