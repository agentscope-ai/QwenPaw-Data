import { utf16Span } from '../compare/textSpans.js';

/**
 * Pure helpers behind the Section-level GT comparison (需求 2).
 *
 * Relative ESM imports keep these helpers executable in Node tests.
 */

/**
 * Draft paths the skill CLI writes, relative to the session artifact dir:
 * `sections/{id}/baseline/draft.md`, `sections/{id}/runs/{period}/draft.md`, plus
 * the pre-`baseline/` legacy layout that `report_cli.py` migrates in place.
 */
const DRAFT_PATH_RE =
  /^report-reproduction\/sections\/([^/]+)\/(?:baseline\/|runs\/([^/]+)\/)?draft\.md$/;

export interface SectionRef {
  sectionId: string;
  /** Present only when the draft came from a cross-period run. */
  period?: string;
}

/**
 * What the compare panel renders, decided by the config panel (方案7 D2).
 *
 * `section` is non-null only when the chosen pair is exactly a reproduction
 * draft against its own GT — then the machine verdict already written to
 * `comparison.json` (numeric detail + the human release trail) is read off disk
 * instead of recomputed. Any other pair recomputes through the generic
 * `POST /files/compare` endpoint, which never writes a file.
 */
export interface CompareSelection {
  /** Artifact-dir-relative reference (GT) path; null leaves it to the report GT. */
  referencePath: string | null;
  /** Artifact-dir-relative target (reproduced draft) path. */
  targetPath: string;
  referenceLabel: string;
  targetLabel: string;
  /** Non-null only for the recommended draft ↔ its-GT pair. */
  section: SectionRef | null;
}

/**
 * Locate the report Section a workspace artifact path belongs to.
 *
 * Used by the draft.md preview footer to decide whether the "compare with the
 * source report" action applies, and which Section (and run period) to open.
 * Returns `null` for anything that is not a reproduction draft — `source.md`,
 * `spec.json`, `comparison.json`, plain sql/csv artifacts.
 */
export function resolveSectionFromPath(relativePath: string | null | undefined): SectionRef | null {
  if (!relativePath) return null;
  const hit = DRAFT_PATH_RE.exec(relativePath);
  if (!hit) return null;
  const sectionId = hit[1];
  if (!sectionId) return null;
  return hit[2] ? { sectionId, period: hit[2] } : { sectionId };
}

/**
 * The period immediately before `period`, for the cross-period recommendation
 * (方案7 D2: reproducing September recommends August).
 *
 * Handles the two period grammars the skill CLI writes — `YYYYMM` months and
 * `YYYY-Www` weeks. A week at the year boundary (W01) and any unrecognised
 * format return `null` rather than guessing, so the recommendation card simply
 * does not appear.
 */
export function previousPeriod(period: string | null | undefined): string | null {
  if (!period) return null;
  const month = /^(\d{4})(\d{2})$/.exec(period);
  if (month) {
    const year = Number(month[1]);
    const index = Number(month[2]);
    if (index <= 1) return `${year - 1}12`;
    return `${year}${String(index - 1).padStart(2, '0')}`;
  }
  const week = /^(\d{4})-W(\d{1,2})$/.exec(period);
  if (week) {
    const year = Number(week[1]);
    const index = Number(week[2]);
    if (index <= 1) return null; // crossing the year boundary: skip, do not guess
    return `${year}-W${String(index - 1).padStart(2, '0')}`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// diff index

/** Mirrors `agent/tools/compare.py:NumericDifference`. */
export interface NumericDifference {
  reference: string;
  target: string | null;
  relative_error: number | null;
  matched: boolean;
  note: string | null;
  target_start?: number | null;
  target_end?: number | null;
}

/** Mirrors `agent/tools/compare.py:StructureDifference`. */
export interface StructureDifference {
  kind: string;
  description: string;
  severity: 'info' | 'warning' | 'error';
}

/**
 * Structural view of `comparison.json` (`api/types.ts:ComparisonFile`). Kept for
 * callers that want to read the payload directly; `buildDiffIndex` narrows at
 * runtime instead, so this module stays import-free.
 */
export interface ComparisonLike {
  passed?: boolean;
  passed_with_review?: boolean;
  numeric_coverage?: number;
  numeric_differences?: NumericDifference[] | null;
  structure_differences?: StructureDifference[] | null;
  criteria_analysis?: unknown[] | null;
  notes?: string | null;
}

export interface DiffRow {
  id: string;
  source: string;
  draft: string | null;
  relativeError: number | null;
  matched: boolean;
  note: string | null;
  /**
   * Character span of `draft` inside the Section draft.md. Null on payloads
   * written before the backend emitted offsets; those rows are never highlighted.
   */
  draftStart: number | null;
  draftEnd: number | null;
}

export interface DiffIndex {
  rows: DiffRow[];
  structure: StructureDifference[];
  notes: string;
  /**
   * `numeric_coverage` from the payload: matched GT numbers over all GT numbers.
   * The diff rows themselves only ever carry mismatches, so this is the only
   * place the matched share survives — the list header reports it instead of a
   * meaningless "matched / rows" ratio.
   */
  coverage: number | null;
}

const EMPTY_DIFF_INDEX: DiffIndex = {
  rows: [],
  structure: [],
  notes: '',
  coverage: null,
};

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * Flatten `comparison.json` into rows plus a draft-side highlight lookup.
 *
 * Takes `unknown` on purpose so every field is narrowed at runtime here instead
 * of forcing callers into an `as` cast on a file that may be half-written.
 *
 * The rows keep the UI's own words rather than the payload's: `source` is the GT
 * side (`reference`) and `draft` the reproduced side (`target`), which is still
 * literally `draft.md` on disk.
 *
 * A `draft` of `null` (GT number with no counterpart) yields a row for the diff
 * list but nothing to highlight, which is correct — there is no draft text to mark.
 */
export function buildDiffIndex(comparison: unknown): DiffIndex {
  if (!comparison || typeof comparison !== 'object') return EMPTY_DIFF_INDEX;
  const raw = comparison as Record<string, unknown>;
  const differences = asArray<NumericDifference>(raw.numeric_differences);
  const structure = asArray<StructureDifference>(raw.structure_differences);
  const notes = typeof raw.notes === 'string' ? raw.notes : '';
  const coverage = typeof raw.numeric_coverage === 'number' ? raw.numeric_coverage : null;

  if (!differences.length && !structure.length) {
    return { ...EMPTY_DIFF_INDEX, structure, notes, coverage };
  }

  const rows: DiffRow[] = [];

  differences.forEach((difference, index) => {
    const source = typeof difference?.reference === 'string' ? difference.reference : '';
    const draft = typeof difference?.target === 'string' ? difference.target : null;
    const row: DiffRow = {
      id: `diff-${index}`,
      source,
      draft,
      relativeError:
        typeof difference?.relative_error === 'number' ? difference.relative_error : null,
      matched: difference?.matched === true,
      note: typeof difference?.note === 'string' ? difference.note : null,
      draftStart: typeof difference?.target_start === 'number' ? difference.target_start : null,
      draftEnd: typeof difference?.target_end === 'number' ? difference.target_end : null,
    };
    rows.push(row);
  });

  return { rows, structure, notes, coverage };
}

/** Structural view of `run_checks.json` (`report_cli.py:SectionRunChecks`). */
export interface RunCheckLike {
  name: string;
  passed: boolean;
  details: string;
}

export interface RunChecksSummary {
  period: string;
  passed: boolean;
  checks: RunCheckLike[];
  notes: string;
}

/** Narrow the loosely typed `run_checks.json` payload into renderable rows. */
export function buildRunChecks(runChecks: unknown): RunChecksSummary | null {
  if (!runChecks || typeof runChecks !== 'object') return null;
  const raw = runChecks as Record<string, unknown>;
  return {
    period: typeof raw.period === 'string' ? raw.period : '',
    passed: raw.passed === true,
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    checks: asArray<Record<string, unknown>>(raw.checks).map((check) => ({
      name: typeof check?.name === 'string' ? check.name : '',
      passed: check?.passed === true,
      details: typeof check?.details === 'string' ? check.details : '',
    })),
  };
}

// ---------------------------------------------------------------------------
// Exact source intervals: source and body positions below use UTF-16.

export interface RetainedRange {
  sourceStart: number; // Inclusive position in the complete source.
  sourceEnd: number; // Exclusive position in the complete source.
  bodyStart: number; // Position of this retained fragment in the rendered body.
}

export interface HighlightSpan {
  id: string; // Stable comparison row identity, independent of its text.
  start: number; // Inclusive UTF-16 body offset.
  end: number; // Exclusive UTF-16 body offset.
}

export function projectDiffSpans(
  text: string,
  rows: DiffRow[],
  retained: RetainedRange[],
): HighlightSpan[] {
  // Backend Path.read_text uses universal newlines: CRLF counts as one code
  // point. Map those boundaries back to the unmodified file served to the UI.
  const boundaries = [0];
  for (let offset = 0; offset < text.length;) {
    if (text[offset] === '\r' && text[offset + 1] === '\n') offset += 2;
    else offset += (text.codePointAt(offset) ?? 0) > 0xffff ? 2 : 1;
    boundaries.push(offset);
  }
  const result: HighlightSpan[] = [];
  for (const row of rows) {
    if (row.matched || !row.draft || row.draftStart === null || row.draftEnd === null) continue;
    const span = utf16Span(boundaries, { start: row.draftStart, end: row.draftEnd });
    if (!span || text.slice(span.start, span.end) !== row.draft) continue;
    const fragment = retained.find(
      (range) => span.start >= range.sourceStart && span.end <= range.sourceEnd,
    );
    if (!fragment) continue; // Removed or crosses a removed narrative block.
    result.push({
      id: row.id,
      start: fragment.bodyStart + span.start - fragment.sourceStart,
      end: fragment.bodyStart + span.end - fragment.sourceStart,
    });
  }
  return result.sort((a, b) => a.start - b.start || a.end - b.end);
}

export const DRAFT_DIFF_HEADING = '复现差异与数据缺口说明';

export interface DraftDiffSplit {
  diff: string | null; // Narrative only, with its sentinel heading removed.
  body: string; // Untouched source fragments concatenated in their original order.
  bodyOffset: number | null; // Legacy suffix offset; use retained for arbitrary placement.
  retained: RetainedRange[]; // Mapping back to the complete original draft.
}

/** Only an exact level-two heading outside fenced code starts the narrative. */
export function splitDraftDiffSection(text: string): DraftDiffSplit {
  let fence: { marker: string; length: number } | null = null;
  let start = -1;
  let narrativeStart = -1;
  let end = text.length;
  for (const match of text.matchAll(/[^\n]*(?:\n|$)/g)) {
    if (!match[0]) continue;
    const line = match[0].replace(/\r?\n$/, '');
    const boundary = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (boundary) {
      if (!fence) fence = { marker: boundary[1][0], length: boundary[1].length };
      else if (
        boundary[1][0] === fence.marker &&
        boundary[1].length >= fence.length &&
        !boundary[2].trim()
      )
        fence = null;
      continue;
    }
    if (fence) continue;
    const heading = /^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$/.exec(line);
    if (!heading) continue;
    const title = heading[2].replace(/[ \t]+#+[ \t]*$/, '');
    if (start < 0 && heading[1].length === 2 && title === DRAFT_DIFF_HEADING) {
      start = match.index;
      narrativeStart = match.index + match[0].length;
    } else if (start >= 0 && heading[1].length <= 2) {
      end = match.index;
      break;
    }
  }
  if (start < 0)
    return {
      diff: null,
      body: text,
      bodyOffset: 0,
      retained: [{ sourceStart: 0, sourceEnd: text.length, bodyStart: 0 }],
    };
  const retained: RetainedRange[] = [];
  if (start > 0) retained.push({ sourceStart: 0, sourceEnd: start, bodyStart: 0 });
  if (end < text.length)
    retained.push({ sourceStart: end, sourceEnd: text.length, bodyStart: start });
  return {
    diff:
      text
        .slice(narrativeStart, end)
        .replace(/<\/?dl-diff>/g, '')
        .trim() || null,
    body: text.slice(0, start) + text.slice(end),
    bodyOffset: start === 0 ? end : null,
    retained,
  };
}

/** Runs have independent checks and must never enter baseline polling. */
export function shouldPollComparison(
  sectionMode: boolean,
  isRun: boolean,
  status?: string,
): boolean {
  return sectionMode && !isRun && status === 'validating';
}
