function contentText(item: unknown): string {
  if (!item || typeof item !== 'object' || !('content' in item)) return '';
  const content = (item as { content?: unknown }).content;
  if (!Array.isArray(content)) return '';

  return content.flatMap((part) => (
    part
    && typeof part === 'object'
    && 'type' in part
    && part.type === 'text'
    && 'text' in part
    && typeof part.text === 'string'
      ? [part.text]
      : []
  )).join('\n').trim();
}

export function extractLatestRuntimeText(input: unknown[]): string {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (
      !item
      || typeof item !== 'object'
      || !('role' in item)
      || item.role !== 'user'
    ) {
      continue;
    }
    const text = contentText(item);
    if (text) return text;
  }
  return '';
}

export function shouldClearAcceptedRuntimeInput(
  currentValue: string,
  submittedText: string,
): boolean {
  return Boolean(submittedText) && currentValue.trim() === submittedText.trim();
}
