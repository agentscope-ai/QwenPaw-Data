import type { SemanticConfigPage } from './semanticConfig';

export type WeaveMode = 'FULL';

export type WeavingTaskStatus = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'DEGRADED' | 'FAILED' | 'KILLED';

export interface ConfirmWeavingParams {
  datasource_id: string;
  task_name: string;
  weave_mode: WeaveMode;
}

export interface SemanticWeavingTask {
  id: number;
  task_id: string;
  task_name: string;
  datasource_id: string;
  datasource_name: string;
  weave_mode: WeaveMode;
  status: WeavingTaskStatus;
  error_msg: string | null;
  created_at: string;
}

export interface SemanticWeavingTaskQueryParams {
  datasource_name?: string;
  task_name?: string;
  page?: number;
  size?: number;
}

export type SemanticWeavingTaskPage = SemanticConfigPage<SemanticWeavingTask>;
