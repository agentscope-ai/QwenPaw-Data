export function withRunningChatClarification<
  T extends { chat: { status: string; enable_clarification?: boolean } },
>(projections: T[], enableClarification: boolean): T[] {
  return projections.map((projection) =>
    projection.chat.status === 'running'
      ? { ...projection, chat: { ...projection.chat, enable_clarification: enableClarification } }
      : projection,
  );
}
