import type { ProfilingStage, ProfilingStagePath } from '@/types/dataProfiling';

export type ProfilingFieldKind = 'text' | 'textarea' | 'number' | 'boolean' | 'list';

export interface ProfilingFieldConfig {
  name: string;
  kind?: ProfilingFieldKind;
  readOnly?: boolean;
  disableOnEdit?: boolean;
  /** 结果列表不展示；新增/编辑表单仍可用 */
  disableOnShow?: boolean;
  required?: boolean;
}

export interface ProfilingStageConfig {
  stage: ProfilingStage;
  path: ProfilingStagePath;
  titleKey: string;
  descriptionKey: string;
  hasRows: boolean;
  canAdd: boolean;
  filterParam?: 'dataset_name' | 'dimension_name' | 'metric_name';
  fields: ProfilingFieldConfig[];
  editable: string[];
}

export const PROFILING_STAGES: ProfilingStageConfig[] = [
  {
    stage: 'BIZ_DOMAIN',
    path: 'biz-domain',
    titleKey: 'dataProfiling.stages.bizDomain',
    descriptionKey: 'dataProfiling.stageDescriptions.bizDomain',
    hasRows: true,
    canAdd: true,
    fields: [
      { name: 'domain_name', required: true },
      { name: 'description', kind: 'textarea' },
    ],
    // member_tables 仅作后端归属证据，不展示、不提交；scope 仅存在于正式表。
    editable: ['domain_name', 'description'],
  },
  {
    stage: 'DATASET',
    path: 'dataset',
    titleKey: 'dataProfiling.stages.dataset',
    descriptionKey: 'dataProfiling.stageDescriptions.dataset',
    hasRows: true,
    canAdd: true,
    filterParam: 'dataset_name',
    fields: [
      { name: 'domain_name', required: true, disableOnEdit: true },
      { name: 'dataset_name', required: true },
      { name: 'description', kind: 'textarea' },
      { name: 'source', kind: 'textarea', required: true },
    ],
    editable: ['domain_name', 'dataset_name', 'description', 'source'],
  },
  {
    stage: 'DATASET_FIELDS',
    path: 'dataset-fields',
    titleKey: 'dataProfiling.stages.datasetFields',
    descriptionKey: 'dataProfiling.stageDescriptions.datasetFields',
    hasRows: true,
    canAdd: true,
    filterParam: 'dataset_name',
    fields: [
      { name: 'dataset_name', required: true, disableOnEdit: true },
      { name: 'field_name', required: true, disableOnEdit: true },
      { name: 'data_type', required: true },
      { name: 'is_primary', kind: 'boolean' },
      { name: 'is_nullable', kind: 'boolean' },
      { name: 'description', kind: 'textarea' },
    ],
    editable: [
      'dataset_name',
      'field_name',
      'data_type',
      'is_primary',
      'is_nullable',
      'description',
    ],
  },
  {
    stage: 'DIMENSION_CALIBER',
    path: 'dimension-caliber',
    titleKey: 'dataProfiling.stages.dimensionCaliber',
    descriptionKey: 'dataProfiling.stageDescriptions.dimensionCaliber',
    hasRows: true,
    canAdd: true,
    filterParam: 'dimension_name',
    fields: [
      { name: 'domain_name', required: true, disableOnEdit: true },
      { name: 'dataset_name', required: true, disableOnEdit: true },
      { name: 'dimension_name', required: true, disableOnEdit: true },
      { name: 'expr', kind: 'textarea', required: true },
      { name: 'value_example', kind: 'textarea' },
    ],
    editable: ['domain_name', 'dataset_name', 'dimension_name', 'expr', 'value_example'],
  },
  {
    stage: 'DIMENSION_MANAGE',
    path: 'dimension-manage',
    titleKey: 'dataProfiling.stages.dimensionManage',
    descriptionKey: 'dataProfiling.stageDescriptions.dimensionManage',
    hasRows: false,
    canAdd: false,
    filterParam: 'dimension_name',
    fields: [
      { name: 'domain_name', readOnly: true },
      { name: 'dimension_name', readOnly: true },
      { name: 'description', kind: 'textarea' },
      { name: 'synonyms', kind: 'textarea' },
      { name: 'dimension_type' },
      { name: 'child_dimensions', kind: 'textarea' },
      { name: 'priority' },
    ],
    editable: ['description', 'synonyms', 'dimension_type', 'child_dimensions', 'priority'],
  },
  {
    stage: 'METRIC_CALIBER',
    path: 'metric-caliber',
    titleKey: 'dataProfiling.stages.metricCaliber',
    descriptionKey: 'dataProfiling.stageDescriptions.metricCaliber',
    hasRows: true,
    canAdd: true,
    filterParam: 'metric_name',
    fields: [
      { name: 'domain_name', required: true },
      { name: 'dataset_name', required: true },
      { name: 'metric_name', required: true },
      { name: 'expr', kind: 'textarea', required: true },
      { name: 'time_window', required: true },
      { name: 'additivity' },
    ],
    editable: ['domain_name', 'dataset_name', 'metric_name', 'expr', 'time_window', 'additivity'],
  },
  {
    stage: 'METRIC_MANAGE',
    path: 'metric-manage',
    titleKey: 'dataProfiling.stages.metricManage',
    descriptionKey: 'dataProfiling.stageDescriptions.metricManage',
    hasRows: false,
    canAdd: false,
    filterParam: 'metric_name',
    fields: [
      { name: 'domain_name', readOnly: true },
      { name: 'metric_name', readOnly: true },
      { name: 'description', kind: 'textarea' },
      { name: 'synonyms', kind: 'textarea' },
      { name: 'priority' },
    ],
    editable: ['description', 'synonyms', 'priority'],
  },
];

export const PROFILING_STAGE_BY_PATH = Object.fromEntries(
  PROFILING_STAGES.map((item) => [item.path, item]),
) as Record<ProfilingStagePath, ProfilingStageConfig>;

export function getStageConfig(path: ProfilingStagePath): ProfilingStageConfig | undefined {
  return PROFILING_STAGE_BY_PATH[path];
}
