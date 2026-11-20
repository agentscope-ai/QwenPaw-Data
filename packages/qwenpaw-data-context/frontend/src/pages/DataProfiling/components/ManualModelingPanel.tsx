import { Button, Card, RobotOutlined, Spin, Typography } from '@/design';
import { Flex, Segmented } from 'antd';
import type { ComponentType, LazyExoticComponent } from 'react';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MODELING_PHASES } from '../modelingPhases';
import type { ManualStepKey } from '../modelingPhases';
import type { ProfilingStagePath } from '@/types/dataProfiling';
import styles from '../DataProfiling.module.less';

const BusinessDomainManagement = lazy(() => import('@/pages/BusinessDomainManagement'));
const DatasetManagement = lazy(() => import('@/pages/DatasetManagement'));
const ColumnManagement = lazy(() => import('@/pages/ColumnManagement'));
const DimensionManagement = lazy(() => import('@/pages/DimensionManagement'));
const DimensionCaliberManagement = lazy(() => import('@/pages/DimensionCaliberManagement'));
const MetricLib = lazy(() => import('@/pages/MetricLib'));
const MetricFormulaLib = lazy(() => import('@/pages/MetricFormulaLib'));

const MANUAL_COMPONENTS: Record<
  ManualStepKey,
  LazyExoticComponent<ComponentType<{ datasourceId?: string }>>
> = {
  'business-domain': BusinessDomainManagement,
  dataset: DatasetManagement,
  'dataset-fields': ColumnManagement,
  dimension: DimensionManagement,
  'dimension-caliber': DimensionCaliberManagement,
  metric: MetricLib,
  'metric-caliber': MetricFormulaLib,
};

const MANUAL_MODELING_TITLE_STYLE = { margin: '0 0 4px', fontSize: 14 } as const;
const MANUAL_MODELING_DESCRIPTION_STYLE = { fontSize: 12 } as const;

interface ManualModelingPanelProps {
  phaseIndex: number;
  datasourceId: string;
  canUseAi: boolean;
  isAiStageEnabled: (stagePath: ProfilingStagePath) => boolean;
  getAiActionLabel: (stagePath: ProfilingStagePath) => string;
  onOpenAi: (stagePath: ProfilingStagePath) => void;
  showPhaseAction: boolean;
  previousPhaseDisabled: boolean;
  onPreviousPhase: () => void;
  onSaveExit: () => void;
  phaseActionLabel: string;
  phaseActionHint: string;
  phaseActionLoading: boolean;
  phaseActionDisabled: boolean;
  onPhaseAction: () => void;
}

export default function ManualModelingPanel({
  phaseIndex,
  datasourceId,
  canUseAi,
  isAiStageEnabled,
  getAiActionLabel,
  onOpenAi,
  showPhaseAction,
  previousPhaseDisabled,
  onPreviousPhase,
  onSaveExit,
  phaseActionLabel,
  phaseActionHint,
  phaseActionLoading,
  phaseActionDisabled,
  onPhaseAction,
}: ManualModelingPanelProps) {
  const { t } = useTranslation();
  const phase = MODELING_PHASES[phaseIndex] ?? MODELING_PHASES[0];
  const [activeStep, setActiveStep] = useState<ManualStepKey>(phase.manualSteps[0].key);

  useEffect(() => {
    setActiveStep(phase.manualSteps[0].key);
  }, [phase]);

  const options = useMemo(
    () =>
      phase.manualSteps.map((step) => ({
        label: t(step.labelKey),
        value: step.key,
      })),
    [phase.manualSteps, t],
  );
  const ActiveComponent = MANUAL_COMPONENTS[activeStep];
  const activeManualStep =
    phase.manualSteps.find((step) => step.key === activeStep) ?? phase.manualSteps[0];
  const activeStepIndex = phase.manualSteps.findIndex((step) => step.key === activeManualStep.key);
  const nextManualStep = phase.manualSteps[activeStepIndex + 1];
  const isFirstManualStep = activeStepIndex <= 0;
  const isLastManualStep = !nextManualStep;
  const primaryActionLabel = nextManualStep
    ? t('dataProfiling.phaseActions.enterStep', { next: t(nextManualStep.labelKey) })
    : phaseActionLabel;
  const activeActionHint = nextManualStep
    ? t('dataProfiling.phaseActionHints.nextStep', { next: t(nextManualStep.labelKey) })
    : phaseActionHint;

  const handlePrevious = () => {
    if (!isFirstManualStep) {
      setActiveStep(phase.manualSteps[activeStepIndex - 1].key);
      return;
    }
    onPreviousPhase();
  };

  const handlePrimaryAction = () => {
    if (nextManualStep) {
      setActiveStep(nextManualStep.key);
      return;
    }
    onPhaseAction();
  };

  return (
    <Card className={styles.manualModelingCard}>
      <Flex justify="space-between" align="center" gap={16} wrap>
        <div>
          <Typography.Title level={5} style={MANUAL_MODELING_TITLE_STYLE}>
            {t('dataProfiling.manualModeling')}
          </Typography.Title>
          <Typography.Text type="secondary" style={MANUAL_MODELING_DESCRIPTION_STYLE}>
            {t('dataProfiling.manualModelingDescription')}
          </Typography.Text>
        </div>
        {canUseAi ? (
          <Button
            type="primary"
            ghost
            icon={<RobotOutlined />}
            disabled={!isAiStageEnabled(activeManualStep.stagePath)}
            onClick={() => onOpenAi(activeManualStep.stagePath)}
          >
            {getAiActionLabel(activeManualStep.stagePath)}
          </Button>
        ) : null}
      </Flex>

      {options.length > 1 ? (
        <Segmented
          className={styles.manualStepSelector}
          options={options}
          value={activeStep}
          onChange={(value: string | number) => setActiveStep(value as ManualStepKey)}
        />
      ) : null}

      <div className={styles.manualModelingContent}>
        <Suspense
          fallback={
            <div className={styles.manualModelingLoading}>
              <Spin />
            </div>
          }
        >
          <ActiveComponent key={`${datasourceId}:${activeStep}`} datasourceId={datasourceId} />
        </Suspense>
      </div>

      {showPhaseAction ? (
        <div className={styles.manualModelingFooter}>
          <Button
            disabled={phaseActionLoading || (isFirstManualStep && previousPhaseDisabled)}
            onClick={handlePrevious}
          >
            {t('dataProfiling.phaseActions.previous')}
          </Button>
          <Typography.Text type="secondary" className={styles.phaseActionHint}>
            {activeActionHint}
          </Typography.Text>
          <div className={styles.manualModelingFooterActions}>
            <Button disabled={phaseActionLoading} onClick={onSaveExit}>
              {t('dataProfiling.phaseActions.saveAndExit')}
            </Button>
            <Button
              type="primary"
              loading={phaseActionLoading && isLastManualStep}
              disabled={phaseActionLoading || (isLastManualStep && phaseActionDisabled)}
              onClick={handlePrimaryAction}
            >
              {primaryActionLabel}
            </Button>
          </div>
        </div>
      ) : null}
    </Card>
  );
}
