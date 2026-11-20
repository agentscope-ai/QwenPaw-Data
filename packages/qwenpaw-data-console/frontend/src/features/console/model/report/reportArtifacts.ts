import { ApiRequestError } from '@/services/request';
import { dataConsoleApi } from '../../api/client';
import { isMissingArtifact, parseArtifactJson } from './artifactContent';
import type {
  ComparisonFile,
  ReportManifest,
  ReportRunManifest,
  ReportScheduleDescriptor,
  RunChecksFile,
} from '../../api/types';

/**
 * Reading the report-reproduction artifacts (方案6 F1).
 *
 * The host no longer exposes a report REST API: the skill's own
 * `scripts/report_cli.py` writes every product as a plain file under
 * `report-reproduction/` inside the session artifact dir, and the frontend reads
 * them through the generic `GET /sessions/{sid}/files/access` endpoint. So this
 * module is the only place that knows both halves of that contract — where each
 * artifact lives, and how a missing one is reported.
 *
 * Every reader resolves a missing artifact to `null` instead of throwing: the
 * console polls these files while the agent is still producing them, so "not
 * written yet" is the normal state rather than an error. Anything else (an
 * escaping path, a dead session) still rejects, and callers surface it.
 */

export const REPORT_PRODUCT_DIR = 'report-reproduction';

const MANIFEST_FILE = 'report_manifest.json';
const COMPARISON_FILE = 'comparison.json';
const RUN_CHECKS_FILE = 'run_checks.json';
const RUN_MANIFEST_FILE = 'run_manifest.json';
const SCHEDULE_DESCRIPTOR_FILE = 'schedule_descriptor.json';
const COMPILED_REPORT_FILE = 'report.md';
const DRAFT_FILE = 'draft.md';
const SOURCE_FILE = 'source.md';

// ---------------------------------------------------------------------------
// artifact paths (relative to the session artifact dir)

export function reportManifestPath(): string {
  return `${REPORT_PRODUCT_DIR}/${MANIFEST_FILE}`;
}

export function sectionSourcePath(sectionId: string): string {
  return `${REPORT_PRODUCT_DIR}/sections/${sectionId}/${SOURCE_FILE}`;
}

export function sectionComparisonPath(sectionId: string): string {
  return `${REPORT_PRODUCT_DIR}/sections/${sectionId}/baseline/${COMPARISON_FILE}`;
}

export function baselineDraftPath(sectionId: string): string {
  return `${REPORT_PRODUCT_DIR}/sections/${sectionId}/baseline/${DRAFT_FILE}`;
}

export function runDraftPath(sectionId: string, period: string): string {
  return `${REPORT_PRODUCT_DIR}/sections/${sectionId}/runs/${period}/${DRAFT_FILE}`;
}

export function runChecksPath(sectionId: string, period: string): string {
  return `${REPORT_PRODUCT_DIR}/sections/${sectionId}/runs/${period}/${RUN_CHECKS_FILE}`;
}

export function runManifestPath(period: string): string {
  return `${REPORT_PRODUCT_DIR}/runs/${period}/${RUN_MANIFEST_FILE}`;
}

/** The whole reproduced document; `period` selects one scheduled run's copy. */
export function compiledReportPath(period?: string | null): string {
  return period
    ? `${REPORT_PRODUCT_DIR}/runs/${period}/${COMPILED_REPORT_FILE}`
    : `${REPORT_PRODUCT_DIR}/${COMPILED_REPORT_FILE}`;
}

export function scheduleDescriptorPath(): string {
  return `${REPORT_PRODUCT_DIR}/${SCHEDULE_DESCRIPTOR_FILE}`;
}

/**
 * The materialised GT document. `manifest.source_path` is product-relative
 * (`source/original.html`), so it needs the product dir prefixed before it can
 * be handed to the access endpoint.
 */
export function sourceDocumentPath(manifest: ReportManifest): string {
  return `${REPORT_PRODUCT_DIR}/${manifest.source_path}`;
}

// ---------------------------------------------------------------------------
// generic readers

/** Read one artifact as text; `null` when it does not exist yet. */
export async function readArtifactText(
  sessionId: string,
  relativePath: string,
): Promise<string | null> {
  try {
    const response = await dataConsoleApi.getWorkspaceFile(sessionId, relativePath, 'preview');
    return await response.text();
  } catch (error) {
    if (error instanceof ApiRequestError && isMissingArtifact(error)) return null;
    throw error;
  }
}

/**
 * Read one artifact as JSON; `null` when it does not exist yet.
 *
 * Parsed from text rather than `response.json()` because the endpoint serves the
 * file with whatever MIME type its extension implies. Invalid JSON remains an
 * error, with the artifact path included, rather than being treated as absent.
 */
export async function readArtifactJson<T>(
  sessionId: string,
  relativePath: string,
): Promise<T | null> {
  const text = await readArtifactText(sessionId, relativePath);
  return parseArtifactJson<T>(text, relativePath);
}

// ---------------------------------------------------------------------------
// report readers

export function readReportManifest(sessionId: string): Promise<ReportManifest | null> {
  return readArtifactJson<ReportManifest>(sessionId, reportManifestPath());
}

export function readRunManifest(
  sessionId: string,
  period: string,
): Promise<ReportRunManifest | null> {
  return readArtifactJson<ReportRunManifest>(sessionId, runManifestPath(period));
}

export function readScheduleDescriptor(
  sessionId: string,
): Promise<ReportScheduleDescriptor | null> {
  return readArtifactJson<ReportScheduleDescriptor>(sessionId, scheduleDescriptorPath());
}

/** The compiled document written by `report_cli.py compile-report`. */
export function readCompiledReport(
  sessionId: string,
  period?: string | null,
): Promise<string | null> {
  return readArtifactText(sessionId, compiledReportPath(period));
}

export interface SourceDocument {
  kind: 'html' | 'markdown';
  content: string;
  /** Artifact-relative path, kept only for the HTML case (it is previewable). */
  path: string | null;
}

function extensionOf(path: string): string {
  const index = path.lastIndexOf('.');
  return index < 0 ? '' : path.slice(index + 1).toLowerCase();
}

function trimEnd(text: string): string {
  return text.replace(/\s+$/, '');
}

/**
 * The whole GT document, in the shape the preview panes already render.
 *
 * HTML is handed over verbatim so the iframe keeps the original charts; `.md`
 * and `.txt` are read as Markdown. A binary GT (`.docx`, `.pdf`) cannot be
 * rendered as-is, so — exactly like the endpoint this replaced — the parsed
 * per-Section `source.md` files are concatenated into one Markdown document.
 */
export async function readSourceDocument(
  sessionId: string,
  manifest?: ReportManifest | null,
): Promise<SourceDocument | null> {
  const resolved = manifest === undefined ? await readReportManifest(sessionId) : manifest;
  if (!resolved) return null;

  const path = sourceDocumentPath(resolved);
  const extension = extensionOf(resolved.source_path);
  if (extension === 'html' || extension === 'htm') {
    const content = await readArtifactText(sessionId, path);
    if (content !== null) return { kind: 'html', content, path };
  } else if (extension === 'md' || extension === 'markdown' || extension === 'txt') {
    const content = await readArtifactText(sessionId, path);
    if (content !== null) return { kind: 'markdown', content, path: null };
  }

  const parts = await Promise.all(
    resolved.sections.map(({ section }) =>
      readArtifactText(sessionId, `${REPORT_PRODUCT_DIR}/${section.source_path}`),
    ),
  );
  const bodies = parts.filter((part): part is string => Boolean(part && part.trim()));
  if (!bodies.length) return null;
  return {
    kind: 'markdown',
    content: `${trimEnd(
      [`# ${resolved.title}`, ...bodies.map((body) => body.trim())].join('\n\n'),
    )}\n`,
    path: null,
  };
}

export interface SectionComparisonView {
  sectionId: string;
  title: string;
  status: string;
  mode: 'baseline' | 'run';
  period: string | null;
  /** Section GT text (`sections/{id}/source.md`), `''` before `parse` ran. */
  source: string;
  /** Reproduced draft text, `''` before the section was generated. */
  draft: string;
  /** Baseline mode only: a run has no GT to compare against. */
  comparison: ComparisonFile | null;
  /** Run mode only: business checks instead of numeric differences. */
  runChecks: RunChecksFile | null;
}

/**
 * Assemble one Section's comparison view from the files on disk.
 *
 * Replaces the removed aggregate endpoint: the same fields, but read as three or
 * four parallel artifact fetches. `null` means there is no report manifest at
 * all. Report discovery uses registered artifacts; this nullable result also
 * handles a manifest removed between registration and the read.
 */
export async function readSectionComparison(
  sessionId: string,
  sectionId: string,
  period?: string | null,
  draftPath?: string,
): Promise<SectionComparisonView | null> {
  const manifest = await readReportManifest(sessionId);
  if (!manifest) return null;

  const runPeriod = period ?? null;
  const progress = manifest.sections.find(({ section }) => section.section_id === sectionId);

  const [source, draft, comparison, runManifest, runChecks] = await Promise.all([
    readArtifactText(sessionId, sectionSourcePath(sectionId)),
    readArtifactText(
      sessionId,
      draftPath ?? (runPeriod ? runDraftPath(sectionId, runPeriod) : baselineDraftPath(sectionId)),
    ),
    runPeriod
      ? Promise.resolve(null)
      : readArtifactJson<ComparisonFile>(sessionId, sectionComparisonPath(sectionId)),
    runPeriod ? readRunManifest(sessionId, runPeriod) : Promise.resolve(null),
    runPeriod
      ? readArtifactJson<RunChecksFile>(sessionId, runChecksPath(sectionId, runPeriod))
      : Promise.resolve(null),
  ]);

  const status = runPeriod
    ? (runManifest?.sections.find((entry) => entry.section_id === sectionId)?.status ?? 'pending')
    : (progress?.status ?? 'pending');

  return {
    sectionId,
    title: progress?.section.title ?? sectionId,
    status,
    mode: runPeriod ? 'run' : 'baseline',
    period: runPeriod,
    source: source ?? '',
    draft: draft ?? '',
    comparison,
    runChecks,
  };
}
