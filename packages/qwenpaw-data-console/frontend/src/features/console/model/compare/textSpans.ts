export interface TextSpan {
  start: number; // Inclusive position.
  end: number; // Exclusive position.
}

/** Convert Python code point offsets to JavaScript UTF-16 boundaries once. */
export function codePointBoundaries(text: string): number[] {
  const offsets = [0];
  let offset = 0;
  for (const character of text) {
    offset += character.length;
    offsets.push(offset);
  }
  return offsets;
}

export function utf16Span(boundaries: readonly number[], span: TextSpan): TextSpan | null {
  if (
    !Number.isInteger(span.start) ||
    !Number.isInteger(span.end) ||
    span.start < 0 ||
    span.end <= span.start ||
    span.end >= boundaries.length
  )
    return null;
  return { start: boundaries[span.start], end: boundaries[span.end] };
}

/** Split existing text using backend ranges; never search for matching values. */
export function splitTextSpans(text: string, spans: readonly TextSpan[]) {
  const boundaries = codePointBoundaries(text);
  const ranges = spans
    .map((span) => utf16Span(boundaries, span))
    .filter((span): span is TextSpan => span !== null)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: TextSpan[] = [];
  for (const range of ranges) {
    const previous = merged[merged.length - 1];
    if (previous && range.start <= previous.end) previous.end = Math.max(previous.end, range.end);
    else merged.push({ ...range });
  }
  const parts: Array<{ text: string; highlighted: boolean; start: number }> = [];
  let offset = 0;
  for (const range of merged) {
    if (offset < range.start)
      parts.push({ text: text.slice(offset, range.start), highlighted: false, start: offset });
    parts.push({ text: text.slice(range.start, range.end), highlighted: true, start: range.start });
    offset = range.end;
  }
  if (offset < text.length)
    parts.push({ text: text.slice(offset), highlighted: false, start: offset });
  return parts;
}
