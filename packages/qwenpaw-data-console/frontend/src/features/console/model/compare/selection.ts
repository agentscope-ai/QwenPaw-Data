import type { ReportManifest } from '../../api/types.js';
import { resolveSectionFromPath, type CompareSelection } from '../report/sectionDiff.js';
import { isComparable } from './families.js';

/** Preserve the report context only for its own default reference/target pair. */
export function defaultReportSelection(
  manifest: ReportManifest | null,
  targetPath: string,
  targetLabel: string,
): CompareSelection {
  const section = resolveSectionFromPath(targetPath);
  let referencePath = manifest ? `report-reproduction/${manifest.source_path}` : null;
  if (manifest && referencePath && !isComparable(referencePath)) {
    const source = manifest.sections.find(
      (entry) => entry.section.section_id === section?.sectionId,
    )?.section.source_path;
    referencePath = source ? `report-reproduction/${source}` : null;
  }
  return {
    referencePath,
    targetPath,
    referenceLabel: manifest?.title || 'GT',
    targetLabel,
    section,
  };
}

export function selectedComparison(
  initial: CompareSelection,
  referencePath: string,
  referenceLabel: string,
): CompareSelection {
  return {
    ...initial,
    referencePath,
    referenceLabel,
    section: referencePath === initial.referencePath ? initial.section : null,
  };
}
