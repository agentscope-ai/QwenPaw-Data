export type ProfilingStage =
  | 'BIZ_DOMAIN'
  | 'DATASET'
  | 'DATASET_FIELDS' // 数据集列探查阶段
  | 'DIMENSION_CALIBER'
  | 'DIMENSION_MANAGE'
  | 'METRIC_CALIBER'
  | 'METRIC_MANAGE';

export type ProfilingStagePath =
  | 'biz-domain'
  | 'dataset'
  | 'dataset-fields' // 数据集列探查阶段 URL path
  | 'dimension-caliber'
  | 'dimension-manage'
  | 'metric-caliber'
  | 'metric-manage';

export type ProfilingCandidateStagePath = Exclude<
  ProfilingStagePath,
  'dimension-manage' | 'metric-manage'
>;

export type ProfilingStageStatus =
  'NOT_STARTED' | 'PROBING' | 'REVIEWING' | 'COMPLETED' | 'SKIPPED';

export type ProfilingOverallStatus = 'NOT_STARTED' | 'IN_PROGRESS' | 'COMPLETED';
export type ProfilingTaskStatus = 'NOT_STARTED' | 'RUNNING' | 'SUCCESS' | 'STOPPED' | 'FAILED';
export type ProfilingRowStatus = 'PENDING' | 'CONFIRMED' | 'SKIPPED';
/** 本轮产出方式：算法新增（create）还是在既有草稿上完善（refine），仅暂存态可见 */
export type ProfilingRowOrigin = 'create' | 'refine';

export interface ProfilingStageActionResponse {
  stage: ProfilingStage;
  status: ProfilingStageStatus;
  task_id?: string | null;
  promoted?: number;
  success?: boolean;
  imported?: number;
  stopped?: boolean;
}

export interface ProfilingBatchActionResponse {
  confirmed?: number;
  deleted?: number;
  skipped?: number; // 本次批量跳过的候选行数
}

export interface ProfilingOverviewRecord {
  datasource_id: string;
  datasource_name?: string | null;
  datasource_type?: string | null;
  overall_status: ProfilingOverallStatus;
  biz_domain_status: ProfilingStageStatus;
  dataset_status: ProfilingStageStatus;
  dataset_fields_status: ProfilingStageStatus; // 数据集列阶段状态
  dimension_caliber_status: ProfilingStageStatus;
  dimension_manage_status: ProfilingStageStatus;
  metric_caliber_status: ProfilingStageStatus;
  metric_manage_status: ProfilingStageStatus;
}

export interface ProfilingOverviewParams {
  [key: string]: unknown;
  datasource_id?: string;
  datasource_name?: string;
  datasource_type?: string;
  page?: number;
  size?: number;
}

export interface ProfilingStageStatusItem {
  stage: ProfilingStage;
  status: ProfilingStageStatus;
  current_task_id?: string | null;
}

export interface ProfilingStatusResponse {
  datasource_id: string;
  overall_status: ProfilingOverallStatus;
  stages: ProfilingStageStatusItem[];
}

export interface ProfilingTask {
  task_id?: string | null;
  datasource_id: string;
  stage: ProfilingStage;
  status: ProfilingTaskStatus;
  progress: number;
  message?: string | null;
  error_msg?: string | null;
}

export interface ProfilingResultRow {
  id: number;
  datasource_id: string;
  task_id?: string | null;
  source_type?: 'ALGO' | 'HUMAN';
  origin?: ProfilingRowOrigin;
  probe_status?: ProfilingRowStatus;
  [key: string]: unknown;
}

export interface ProfilingPage<T> {
  records: T[];
  total: number;
  page: number;
  size: number;
}
