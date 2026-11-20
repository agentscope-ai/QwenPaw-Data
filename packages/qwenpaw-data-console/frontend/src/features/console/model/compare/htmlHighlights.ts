/** Apply only exact backend-projected text spans, never search by numeric value. */
export function highlightComparedHtml(
  html: string,
  spans: readonly { id: string; start: number; end: number }[] = [],
): string {
  if (spans.length === 0) return html;
  const boundaries = [0];
  for (let offset = 0; offset < html.length;) {
    if (html[offset] === '\r' && html[offset + 1] === '\n') offset += 2;
    else offset += (html.codePointAt(offset) ?? 0) > 0xffff ? 2 : 1;
    boundaries.push(offset);
  }
  let cursor = 0;
  const result: string[] = [];
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    const start = boundaries[span.start];
    const end = boundaries[span.end];
    if (
      !/^diff-\d+$/.test(span.id) ||
      start === undefined ||
      end === undefined ||
      start < cursor ||
      end <= start ||
      /[<>]/.test(html.slice(start, end))
    )
      continue;
    result.push(
      html.slice(cursor, start),
      `<mark data-qwenpaw-data-diff-id="${span.id}" style="background:#fff1b8;color:#873800;outline:1px solid #ffc53d;cursor:pointer;border-radius:3px">`,
      html.slice(start, end),
      '</mark>',
    );
    cursor = end;
  }
  result.push(html.slice(cursor));
  return result.join('');
}
