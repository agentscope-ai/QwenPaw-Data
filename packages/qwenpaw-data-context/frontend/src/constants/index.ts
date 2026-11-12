import type { WeaveMode, WeavingTaskStatus } from '@/types/semanticWeaving';
import type { DimensionPriority } from '@/types/dimensionManagement';
import type { MetricAdditivity, SemanticPriority, SemanticYesNo } from '@/types/semanticConfig';

export const SEMANTIC_PRIORITY_OPTIONS = [
  { label: 'HIGH', value: 'HIGH' },
  { label: 'MEDIUM', value: 'MEDIUM' },
  { label: 'LOW', value: 'LOW' },
] satisfies Array<{ label: SemanticPriority; value: SemanticPriority }>;

export const SEMANTIC_YES_NO_OPTIONS = [
  { label: 'Y', value: 'Y' },
  { label: 'N', value: 'N' },
] satisfies Array<{ label: SemanticYesNo; value: SemanticYesNo }>;

export const METRIC_ADDITIVITY_OPTIONS = [
  { label: 'ADDITIVE', value: 'ADDITIVE' },
  { label: 'SEMI_ADDITIVE', value: 'SEMI_ADDITIVE' },
  { label: 'NON_ADDITIVE', value: 'NON_ADDITIVE' },
] satisfies Array<{ label: MetricAdditivity; value: MetricAdditivity }>;

export const DIMENSION_PRIORITY_OPTIONS = [
  { label: 'HIGH', value: 'high' },
  { label: 'NORMAL', value: 'normal' },
] satisfies Array<{ label: string; value: DimensionPriority }>;

// 维度类型
export const DIMENSION_TYPE = [
  { value: 'categorical', label: '类别' },
  { value: 'time', label: '时间' },
];

export const DATA_TYPE_OPTIONS = [
  { label: 'STRING', value: 'STRING' },
  { label: 'INT', value: 'INT' },
  { label: 'BIGINT', value: 'BIGINT' },
  { label: 'DOUBLE', value: 'DOUBLE' },
  { label: 'FLOAT', value: 'FLOAT' },
  { label: 'BOOLEAN', value: 'BOOLEAN' },
  { label: 'DATE', value: 'DATE' },
  { label: 'DATETIME', value: 'DATETIME' },
  { label: 'TIMESTAMP', value: 'TIMESTAMP' },
  { label: 'DECIMAL', value: 'DECIMAL' },
  { label: 'VARCHAR', value: 'VARCHAR' },
];

// 步骤配置
export const STEPS = [
  { title: '从业务库拉取', key: 'fetch' },
  { title: '维度推理', key: 'dimension' },
  { title: '样本补全', key: 'sample' },
];

// 数据集类型
export const DATASET_TYPE_OPTIONS = [
  { label: 'ODS', value: 'ODS' },
  { label: 'DWD', value: 'DWD' },
  { label: 'DWS', value: 'DWS' },
  { label: 'OLAP', value: 'OLAP' },
];

// 列类型
export const COLUMN_TYPE_OPTIONS = [
  { label: '维度', value: '维度' },
  { label: '度量', value: '度量' },
  { label: '普通列', value: '普通列' },
];

// 指标时间范围
export const DATE_RANGE_OPTIONS = [
  { label: '日', value: '日' },
  { label: '周', value: '周' },
  { label: '月', value: '月' },
  { label: '近30天', value: '近30天' },
];

// 编织模式
export const WEAVE_MODE_OPTIONS = [{ label: 'FULL', value: 'FULL' }] satisfies Array<{
  label: WeaveMode;
  value: WeaveMode;
}>;

export const TASK_STATUS_COLOR_MAP: Record<WeavingTaskStatus, string> = {
  QUEUED: '#faad14',
  RUNNING: '#0D76FD',
  SUCCESS: '#52c41a',
  DEGRADED: '#fa8c16',
  FAILED: '#ff4d4f',
  KILLED: '#999',
};
