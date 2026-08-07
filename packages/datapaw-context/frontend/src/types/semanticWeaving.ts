// 发起编织任务请求参数
export interface ConfirmWeavingParams {
  datasource_id: string;
  task_name: string;
  weave_mode: string;
}

// 编织任务状态
export type WeavingTaskStatus = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'KILLED';

// 编织任务项
export interface SemanticWeavingTask {
  id: number;
  task_id: string;
  task_name: string;
  datasource_id: string;
  datasource_name?: string;
  weave_mode: string;
  status: WeavingTaskStatus;
  error_msg?: string;
  created_at?: string;
}

// 任务列表查询参数
export interface SemanticWeavingTaskQueryParams {
  datasource_name?: string;
  task_name?: string;
  page?: number;
  size?: number;
}
