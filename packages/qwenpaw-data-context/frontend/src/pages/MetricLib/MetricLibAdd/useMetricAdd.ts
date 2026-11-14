/** The V3 metric batch wizard is disabled by the V4 route redirect. */
export interface MetricItem {
  id: string;
  metricName: string;
  domain: string;
  synonyms: string;
  formula?: string;
  dateRange?: string;
  dataset?: string;
  formulaEvidence?: string;
  derivedFrom?: string;
  evidenceExt?: string;
  dataset_name?: string;
  dataset_id?: string;
}

export const METRIC_ADD_STEPS = [
  { titleKey: 'metricAdd.steps.input', descriptionKey: 'metricAdd.stepDescriptions.input' },
  { titleKey: 'metricAdd.steps.confirm', descriptionKey: 'metricAdd.stepDescriptions.confirm' },
  { titleKey: 'metricAdd.steps.formula', descriptionKey: 'metricAdd.stepDescriptions.formula' },
] as const;

export const generateMetricId = () =>
  `metric_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
