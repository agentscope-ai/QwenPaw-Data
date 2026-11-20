const BOLD_SPAN_PATTERN =
  /<span\b[^>]*class=(?:"[^"]*\bfont-bold\b[^"]*"|'[^']*\bfont-bold\b[^']*')[^>]*>([\s\S]*?)<\/span>/gi;
const SPAN_TAG_PATTERN = /<\/?span\b[^>]*>/gi;

function normalizeSingleLineList(content: string): string {
  if (content.includes('\n')) return content;

  if (/^[-*+]\s/.test(content)) {
    return content.replace(/\s+(?=[-*+]\s+)/g, '\n');
  }

  if (/^\d+[.)]\s/.test(content)) {
    return content.replace(/\s+(?=\d+[.)]\s+)/g, '\n');
  }

  return content;
}

/**
 * Segment fields are authored as Markdown, but older snapshots may contain
 * single-line list syntax and Tailwind-styled span tags. Normalize those
 * legacy shapes before passing the text to the shared Markdown renderer.
 */
export function formatSegmentMarkdown(value: string | null | undefined, fallback: string): string {
  const content = value?.trim();
  if (!content) return fallback;

  const markdown = content.replace(BOLD_SPAN_PATTERN, '**$1**').replace(SPAN_TAG_PATTERN, '');

  return normalizeSingleLineList(markdown);
}
