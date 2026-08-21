import { useState, useCallback } from 'react';
import { message } from '@/design';
import { useNavigate } from 'react-router';
import { confirmCreateMetric, checkDuplicates } from '@/services/metricLib';
import type { MetricLibItem, MetricCheckResult } from '@/types/metricLib';
import { createMetricFormulaBatch } from '@/services/metricFormulaLib';
import { useTranslation } from 'react-i18next';
import { normalizeListResponse } from '@/utils/listResponse';
import { omitKeys } from '@/utils';

// 指标数据类型
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

// 步骤配置
export const METRIC_ADD_STEPS = [
  { titleKey: 'metricAdd.steps.input', descriptionKey: 'metricAdd.stepDescriptions.input' },
  { titleKey: 'metricAdd.steps.confirm', descriptionKey: 'metricAdd.stepDescriptions.confirm' },
  { titleKey: 'metricAdd.steps.formula', descriptionKey: 'metricAdd.stepDescriptions.formula' },
] as const;

export const TOTAL_STEPS = METRIC_ADD_STEPS.length;

// 生成唯一ID
export const generateMetricId = () =>
  `metric_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

/**
 * MetricLibAdd 页面状态管理 Hook
 * 从 zustand store 迁移而来，使用本地 React 状态
 */
export function useMetricAdd() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // 步骤相关状态
  const [currentStep, setCurrentStep] = useState(0);
  // 确认创建后的数据
  const [confirmData, setConfirmData] = useState<MetricItem[]>([]);

  // 表格数据相关状态
  const [dataSource, setDataSource] = useState<MetricItem[]>([]);
  const [editableKeys, setEditableKeys] = useState<React.Key[]>([]);

  // 下一步
  const nextStep = useCallback(() => {
    setCurrentStep((prev) => {
      if (prev < TOTAL_STEPS - 1) {
        return prev + 1;
      }
      return prev;
    });
  }, []);

  // 上一步
  const prevStep = useCallback(() => {
    setCurrentStep((prev) => {
      if (prev > 0) {
        return prev - 1;
      }
      return prev;
    });
  }, []);

  // 设置步骤
  const setStep = useCallback((step: number) => {
    if (step >= 0 && step < TOTAL_STEPS) {
      setCurrentStep(step);
    }
  }, []);

  // 重置步骤
  const resetStep = useCallback(() => {
    setCurrentStep(0);
  }, []);

  // 添加行
  const addRow = useCallback(() => {
    const id = generateMetricId();
    if (currentStep < 2) {
      const newRow: MetricItem = {
      id,
      metricName: '',
      domain: '',
      synonyms: '',
    };
    setDataSource((prev) => [...prev, newRow]);
    } else {
      const newRow: MetricItem = {
      id,
      metricName: '',
      domain: '',
      synonyms: '',
      formula: "",
      dateRange: "",
      dataset_id: '',
      dataset_name: '',
      dataset: "",
      formulaEvidence: "",
      derivedFrom: "",
      evidenceExt: ""
    };
    setConfirmData((prev) => [...prev, newRow]);
    }
    setEditableKeys((prev) => [...prev, id]);
  }, [currentStep]);

  // 删除行
  const deleteRow = useCallback((id: string) => {
    if (currentStep < 2) {
      setDataSource((prev) => prev.filter((item) => item.id !== id));
    } else {
      setConfirmData((prev) => prev.filter((item) => item.id !== id));
    }
    setEditableKeys((prev) => prev.filter((key) => key !== id));
  }, [currentStep]);

  // 清空表格
  const clearTable = useCallback(() => {
    if (currentStep < 2) {
      setDataSource([]);
      setEditableKeys([]);
    } else {
      setConfirmData([]);
      setEditableKeys([]);
    }
  }, [currentStep]);

  // 导入数据
  const importData = useCallback((newRows: MetricItem[]) => {
    setDataSource((prev) => [...prev, ...newRows]);
    setEditableKeys((prev) => [...prev, ...newRows.map((r) => r.id)]);
  }, []);

  // 一致性检查
  const consistencyCheck = useCallback(async (
    currentDataSource: MetricItem[]
  ): Promise<{ hasDuplicate: boolean; data?: MetricCheckResult[] }> => {
    // 过滤掉空行
    const validData = currentDataSource.filter(
      (item) => item.metricName.trim() || item.domain.trim()
    );

    if (validData.length === 0) {
      message.warning(t('metricAdd.addMetricDataFirst'));
      return { hasDuplicate: false };
    }

    // 检查必填项
    const invalidRows = validData.filter((item) => !item.metricName.trim());
    if (invalidRows.length > 0) {
      message.error(t('metricAdd.missingMetricNameRows', { count: invalidRows.length }));
      return { hasDuplicate: false };
    }

    // 转换为后端需要的格式（request 会自动做 camelCase → snake_case 转换）
    const requestData = validData.map((item) => ({
      metricName: item.metricName,
      domain: item.domain,
      synonyms: item.synonyms,
    }));

    try {
      const result = await checkDuplicates(
        requestData as unknown as MetricLibItem[]
      );
      const duplicates = normalizeListResponse<MetricCheckResult>(result);
      if (duplicates.length > 0) {
        return { hasDuplicate: true, data: duplicates };
      }
      // 无重复时自动进入下一步
      setCurrentStep((prev) => (prev < TOTAL_STEPS - 1 ? prev + 1 : prev));
      return { hasDuplicate: false };
    } catch {
      // API 错误由响应拦截器统一提示
      return { hasDuplicate: false };
    }
  }, [t]);

  // 确认创建
  const confirmCreate = useCallback(async (
    currentDataSource: MetricItem[],
    type: 'button' | 'modal'
  ): Promise<boolean> => {
    // 过滤有效数据（metricName 非空的行）
    const validData = currentDataSource.filter((item) => item.metricName.trim());

    if (validData.length === 0) {
      message.warning(t('metricAdd.noValidMetricData'));
      return false;
    }

    const requestData = validData.map((item) => ({
      metric_name: item.metricName,
      domain: item.domain,
      synonyms: item.synonyms,
    }));

    try {
      // request 会自动将 camelCase 转为 snake_case
      const response = await confirmCreateMetric(requestData);
      message.success(t('metricAdd.metricCreateSuccess'));

      // 将后端返回的数据转换为前端期望的格式
      const formattedData = normalizeListResponse<Record<string, unknown>>(response).map((item) => ({
        id: String(item.id ?? item.metric_id ?? generateMetricId()),
        metricName: String(item.metric_name ?? item.metricName ?? ''),
        domain: String(item.domain ?? ''),
        synonyms: String(item.synonyms ?? ''),
        formula: String(item.formula ?? ''),
        dateRange: String(item.date_range ?? item.dateRange ?? ''),
        dataset: String(item.dataset ?? ''),
        dataset_id: String(item.dataset_id ?? ''),
        dataset_name: String(item.dataset_name ?? ''),
        formulaEvidence: String(item.formula_evidence ?? item.formulaEvidence ?? ''),
        derivedFrom: String(item.derived_from ?? item.derivedFrom ?? ''),
        evidenceExt: String(item.evidence_ext ?? item.evidenceExt ?? ''),
      }));

      setConfirmData(formattedData);

      // 成功后自动进入下一步
      if (type === 'modal') {
        setCurrentStep((prev) => (prev < TOTAL_STEPS - 1 ? prev + 1 : prev));
      }
      setCurrentStep((prev) => (prev + 1));
      return true;
    } catch {
      // API 错误由响应拦截器统一提示
      return false;
    }
  }, [t]);

  // 确认录入
  const handleConfirmCreateLib = useCallback(async (): Promise<boolean> => {
    try {
      // 删除 id 字段
      const requestData = confirmData.map((item) => ({
        ...omitKeys(item, ['id', 'metricName', 'dateRange', 'formulaEvidence', 'derivedFrom', 'evidenceExt']),
        metric_name: item.metricName,
        date_range: item.dateRange,
        dataset_id: item.dataset_id ? Number(item.dataset_id) : undefined,
        formula_evidence: item.formulaEvidence,
        derived_from: item.derivedFrom,
        evidence_ext: item.evidenceExt,
      }));
      // 确认创建指标库
      await createMetricFormulaBatch(requestData);
      message.success(t('metricAdd.metricLibCreateSuccess'));
      navigate('/metric-lib');
      return true;
    } catch {
      // API 错误由响应拦截器统一提示
      return false;
    }
  }, [confirmData, navigate, t]);

  // 重置所有状态
  const reset = useCallback(() => {
    setCurrentStep(0);
    setDataSource([]);
    setEditableKeys([]);
  }, []);

  return {
    // 状态
    currentStep,
    dataSource,
    editableKeys,

    // setter（供外部使用）
    setCurrentStep,
    setDataSource,
    setEditableKeys,
    setConfirmData,

    // 步骤操作
    nextStep,
    prevStep,
    setStep,
    resetStep,

    // 表格数据操作
    addRow,
    deleteRow,
    clearTable,
    importData,
    confirmData,

    // 业务操作
    consistencyCheck,
    confirmCreate,
    handleConfirmCreateLib,
    reset,
  };
}
