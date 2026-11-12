import type { ProfilingStagePath } from '@/types/dataProfiling';

export type ManualStepKey =
  | 'business-domain'
  | 'dataset'
  | 'dataset-fields'
  | 'dimension'
  | 'dimension-caliber'
  | 'metric'
  | 'metric-caliber';

interface ManualStep {
  key: ManualStepKey;
  labelKey: string;
  stagePath: ProfilingStagePath;
}

export interface ModelingPhase {
  key: 'business-domain' | 'dataset' | 'dimension' | 'metric';
  titleKey: string;
  stagePaths: ProfilingStagePath[];
  manualSteps: ManualStep[];
}

export const MODELING_PHASES: ModelingPhase[] = [
  {
    key: 'business-domain',
    titleKey: 'dataProfiling.modelingPhases.businessDomain',
    stagePaths: ['biz-domain'],
    manualSteps: [
      {
        key: 'business-domain',
        labelKey: 'menu.businessDomainManagement',
        stagePath: 'biz-domain',
      },
    ],
  },
  {
    key: 'dataset',
    titleKey: 'dataProfiling.modelingPhases.dataset',
    stagePaths: ['dataset', 'dataset-fields'],
    manualSteps: [
      { key: 'dataset', labelKey: 'menu.datasetManagement', stagePath: 'dataset' },
      { key: 'dataset-fields', labelKey: 'menu.columnManagement', stagePath: 'dataset-fields' },
    ],
  },
  {
    key: 'dimension',
    titleKey: 'dataProfiling.modelingPhases.dimension',
    stagePaths: ['dimension-caliber', 'dimension-manage'],
    manualSteps: [
      { key: 'dimension', labelKey: 'menu.dimensionManagement', stagePath: 'dimension-manage' },
      {
        key: 'dimension-caliber',
        labelKey: 'menu.dimensionCaliber',
        stagePath: 'dimension-caliber',
      },
    ],
  },
  {
    key: 'metric',
    titleKey: 'dataProfiling.modelingPhases.metric',
    stagePaths: ['metric-caliber', 'metric-manage'],
    manualSteps: [
      { key: 'metric', labelKey: 'menu.metricManagement', stagePath: 'metric-manage' },
      { key: 'metric-caliber', labelKey: 'menu.metricFormula', stagePath: 'metric-caliber' },
    ],
  },
];

export function getModelingPhaseIndex(stagePath: ProfilingStagePath): number {
  const index = MODELING_PHASES.findIndex((phase) => phase.stagePaths.includes(stagePath));
  return index < 0 ? 0 : index;
}
