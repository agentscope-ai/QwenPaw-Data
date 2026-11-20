/** Same-family constraints used by the backend files/diff endpoint. */
export const COMPARE_FAMILIES = {
  markdown: ['md', 'markdown', 'html', 'htm', 'txt'], // Text and rendered reports.
  csv: ['csv', 'tsv'], // Delimited text tables.
  sql: ['sql'], // SQL scripts.
  json: ['json'], // JSON documents.
} as const;

export type CompareFamily = keyof typeof COMPARE_FAMILIES;

function extension(path: string): string {
  const filename = path.slice(path.lastIndexOf('/') + 1);
  const dot = filename.lastIndexOf('.');
  return dot < 0 ? '' : filename.slice(dot + 1).toLowerCase();
}

export function compareFamily(path: string): CompareFamily | null {
  const ext = extension(path);
  for (const family of Object.keys(COMPARE_FAMILIES) as CompareFamily[]) {
    if ((COMPARE_FAMILIES[family] as readonly string[]).includes(ext)) return family;
  }
  return null;
}

export function isComparable(path: string): boolean {
  return compareFamily(path) !== null;
}

export function isHtml(path: string | null): boolean {
  if (!path) return false;
  const ext = extension(path);
  return ext === 'html' || ext === 'htm';
}
