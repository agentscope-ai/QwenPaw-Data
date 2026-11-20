import assert from 'node:assert/strict';
import test from 'node:test';
import type { ReportManifest } from '../../api/types.js';
import { compareFamily, isComparable, isHtml } from '../compare/families.js';
import { defaultReportSelection, selectedComparison } from '../compare/selection.js';

const manifest: ReportManifest = {
  schema_version: 3,
  revision: 1,
  report_id: 'r1',
  title: 'Quarterly report',
  source_path: 'source/original.pdf',
  source_period: null,
  target_period: null,
  baseline_frozen_at: null,
  created_at: '',
  updated_at: '',
  sections: [
    {
      section: {
        section_id: 'revenue',
        title: 'Revenue',
        level: 2,
        source_path: 'sections/revenue/source.md',
        draft_path: 'sections/revenue/baseline/draft.md',
        spec_path: 'sections/revenue/spec.json',
        comparison_path: 'sections/revenue/baseline/comparison.json',
        blocked_by: [],
      },
      status: 'approved',
      attempt: 1,
      updated_at: '',
      error: null,
    },
  ],
};

test('families follow the backend, including case-insensitive extensions', () => {
  for (const ext of ['md', 'markdown', 'HTML', 'htm', 'txt']) {
    assert.equal(compareFamily(`folder/name.${ext}`), 'markdown');
  }
  assert.equal(compareFamily('x.TSV'), 'csv');
  assert.equal(compareFamily('x.csv'), 'csv');
  assert.equal(compareFamily('x.SQL'), 'sql');
  assert.equal(compareFamily('x.json'), 'json');
  assert.equal(isComparable('source.pdf'), false);
  assert.equal(compareFamily('folder.md/no-extension'), null);
  assert.equal(isHtml(null), false);
  assert.equal(isHtml('source.HTML'), true);
});

test('binary GT uses the matching manifest section source and preserves baseline context', () => {
  const selection = defaultReportSelection(
    manifest,
    'report-reproduction/sections/revenue/baseline/draft.md',
    'draft.md',
  );
  assert.equal(selection.referencePath, 'report-reproduction/sections/revenue/source.md');
  assert.deepEqual(selection.section, { sectionId: 'revenue' });
  assert.equal(selection.referenceLabel, 'Quarterly report');
});

test('text GT remains the complete source and Run period survives only its default pair', () => {
  const selection = defaultReportSelection(
    { ...manifest, source_path: 'source/original.HTML' },
    'report-reproduction/sections/revenue/runs/2026-W39/draft.md',
    'draft.md',
  );
  assert.equal(selection.referencePath, 'report-reproduction/source/original.HTML');
  const chosen = selectedComparison(selection, selection.referencePath!, 'GT');
  assert.deepEqual(chosen.section, { sectionId: 'revenue', period: '2026-W39' });
  const changed = selectedComparison(
    selection,
    'report-reproduction/sections/revenue/source.md',
    'Section',
  );
  assert.equal(changed.section, null);
  assert.equal(changed.targetPath, selection.targetPath);
  assert.deepEqual(
    selectedComparison(selection, selection.referencePath!, 'GT').section,
    chosen.section,
  );
});

test('unrelated files never inherit report context or another binary section source', () => {
  assert.equal(defaultReportSelection(manifest, 'query.sql', 'query.sql').section, null);
  assert.equal(defaultReportSelection(manifest, 'query.sql', 'query.sql').referencePath, null);
  assert.equal(
    defaultReportSelection(
      manifest,
      'report-reproduction/sections/unknown/baseline/draft.md',
      'draft.md',
    ).referencePath,
    null,
  );
  assert.equal(defaultReportSelection(null, 'query.sql', 'query.sql').referencePath, null);
});
