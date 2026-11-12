/** Zero-based, half-open range measured in Unicode code points, not UTF-16. */
export interface DiffSpan {
  start: number; // Inclusive position within this side's line text.
  end: number; // Exclusive position within this side's line text.
}

export interface DiffRow {
  type: 'equal' | 'insert' | 'delete' | 'replace'; // Backend row classification.
  ref_no: number | null; // One-based reference line number; null for an empty side.
  tgt_no: number | null; // One-based target line number; null for an empty side.
  ref_text: string; // Original reference line, empty for an absent side.
  tgt_text: string; // Original target line, empty for an absent side.
  ref_spans: DiffSpan[]; // Changed reference ranges; populated only for replace rows.
  tgt_spans: DiffSpan[]; // Changed target ranges; populated only for replace rows.
}

export interface DiffStats {
  equal: number; // Number of unchanged rows.
  insert: number; // Number of inserted rows.
  delete: number; // Number of deleted rows.
  replace: number; // Number of replaced rows.
}

/** Pure response from POST files/diff; statistics cover only returned rows. */
export interface LineDiffResult {
  schema_version: number; // Backend result schema version.
  target_path: string | null; // Target path relative to the artifact directory.
  reference_path: string | null; // Reference path relative to the artifact directory.
  rows: DiffRow[]; // Aligned row pairs supplied by the backend.
  stats: DiffStats; // Counts by row type; differences are insert + delete + replace.
  truncated: boolean; // True when either input exceeded the backend line limit.
  created_at: string; // ISO timestamp recorded by the backend.
}
