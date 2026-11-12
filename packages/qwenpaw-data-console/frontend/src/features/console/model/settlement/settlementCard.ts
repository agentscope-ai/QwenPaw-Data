import type { SettlementCard } from '../../api/settlement';

export const SETTLEMENT_TYPE_LABELS: Record<string, string> = {
  metric_caliber: '指标口径',
  dimension_def: '维度定义',
  column_meaning: '列含义',
  dataset_usage: '数据集用法',
};

export function getSettlementCardTitle(card: SettlementCard): string {
  const fields = card.fields ?? {};
  switch (card.type) {
    case 'metric_caliber':
      return fields.metric_name || SETTLEMENT_TYPE_LABELS.metric_caliber;
    case 'dimension_def':
      return fields.dimension_name || SETTLEMENT_TYPE_LABELS.dimension_def;
    case 'column_meaning':
      return fields.column_name
        ? `${fields.table ? `${fields.table}.` : ''}${fields.column_name}`
        : SETTLEMENT_TYPE_LABELS.column_meaning;
    case 'dataset_usage':
      return fields.use_case || fields.recommended_dataset || SETTLEMENT_TYPE_LABELS.dataset_usage;
    default:
      return SETTLEMENT_TYPE_LABELS[card.type] || card.type;
  }
}
