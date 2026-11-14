import type { SettlementCard } from '../../api/settlement';
import type { MessageKey } from '@/i18n/locales';

export const FIELD_LABEL_KEYS: Record<string, MessageKey> = {
  metric_name: 'assets.field.metric_name',
  caliber: 'assets.field.caliber',
  formula_sql: 'assets.field.formula_sql',
  table: 'assets.field.table',
  domain: 'assets.field.domain',
  dimension_name: 'assets.field.dimension_name',
  bind_column: 'assets.field.bind_column',
  value_samples: 'assets.field.value_samples',
  column_name: 'assets.field.column_name',
  meaning: 'assets.field.meaning',
  use_case: 'assets.field.use_case',
  recommended_dataset: 'assets.field.recommended_dataset',
};

export const TYPE_LABEL_KEYS: Record<string, MessageKey> = {
  metric_caliber: 'assets.metricCaliber',
  dimension_def: 'assets.dimensionDefinition',
  column_meaning: 'assets.columnMeaning',
  dataset_usage: 'assets.datasetUsage',
};

export const FIELD_ORDER: Record<string, string[]> = {
  metric_caliber: ['metric_name', 'caliber', 'domain', 'table', 'formula_sql'],
  dimension_def: ['dimension_name', 'bind_column', 'value_samples', 'domain', 'table'],
  column_meaning: ['column_name', 'meaning', 'table', 'domain'],
  dataset_usage: ['use_case', 'recommended_dataset', 'domain'],
};

export const EDITABLE_FIELDS = new Set([
  'caliber',
  'formula_sql',
  'meaning',
  'value_samples',
  'use_case',
  'recommended_dataset',
]);

export function orderedFieldEntries(card: SettlementCard, fields: Record<string, string>) {
  const preferredOrder = FIELD_ORDER[card.type] ?? [];
  const allKeys = [...preferredOrder, ...Object.keys(fields)];
  return [...new Set(allKeys)]
    .filter((key) => preferredOrder.includes(key) || String(fields[key] ?? '').trim())
    .map((key) => [key, fields[key] ?? ''] as const);
}

export function getMissingFields(card: SettlementCard, fields: Record<string, string>): string[] {
  return (FIELD_ORDER[card.type] ?? Object.keys(card.fields)).filter(
    (key) => !String(fields[key] ?? '').trim(),
  );
}

export function normalizeFields(fields: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(fields).map(([key, value]) => [key, String(value ?? '').trim()]),
  );
}
